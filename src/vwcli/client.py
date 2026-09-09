from __future__ import annotations

import argparse
import contextlib
import copy
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config, safe_chmod
from .constants import (
    _CLONE_STRIP_KEYS,
    BW_SERVE_HOST,
    BW_SERVE_STARTUP_DELAY,
    BW_SERVE_STARTUP_RETRIES,
    BW_SESSION_DEFAULT_TTL,
    BW_STALE_CIPHER_ERR,
    UUID_RE,
    parse_uri,
)
from .exceptions import VwcliError
from .output import Console, Table, item_group_label, render_search_results


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class Client:
    def __init__(self) -> None:
        self.bw_serve_url = os.environ.get("BW_SERVE_URL", "")
        self.bw_serve_proc: subprocess.Popen[str] | None = None
        self.bw_serve_log_path = ""
        self.bw_serve_managed = False
        self.bw_bin_path = shutil.which("bw")
        self.bw_session = os.environ.get("BW_SESSION", "")
        self.bw_session_expires: int = 0  # Unix timestamp; 0 = unknown
        self.config = Config()
        self.config.load(self)

    def need_bw(self) -> None:
        if self.bw_bin_path:
            return
        nvm_dir = Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm")))
        if not (nvm_dir / "nvm.sh").exists():
            raise VwcliError(f"Missing required command: bw (and NVM not found at {nvm_dir / 'nvm.sh'})")

    def bw_list_organizations(self) -> list[dict[str, Any]]:
        if self.bw_serve_url:
            result = self._bw_serve_list("/list/object/organizations", "organizations")
            if result is not None:
                return result
        return self._bw_run_json_list(["list", "organizations"])

    def bw_get_organization(self, org_id: str) -> dict[str, Any]:
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json("GET", f"/object/organization/{org_id}")
                data = response.get("data")
                if self.bw_serve_response_success(response) and isinstance(data, dict):
                    return data
            except VwcliError:
                print(
                    "[warn] bw serve get organization endpoint failed, falling back to CLI.",
                    file=sys.stderr,
                )
        return self._bw_run_json_dict(["get", "organization", org_id])

    def resolve_organization_by_name(self, name: str) -> dict[str, Any]:
        orgs = self.bw_list_organizations()
        matches = [o for o in orgs if str(o.get("name") or "") == name]
        if len(matches) == 0:
            raise VwcliError(f"No organization named '{name}'")
        if len(matches) > 1:
            print(f"Ambiguous organization name '{name}'. Matches:", file=sys.stderr)
            for m in matches:
                print(
                    f"  - {m.get('name')} [id={m.get('id')}]",
                    file=sys.stderr,
                )
            raise VwcliError("Organization name must be unique.")
        return matches[0]

    def resolve_vault_to_org_id(self, vault_name: str) -> str:
        org = self.resolve_organization_by_name(vault_name)
        org_id = org.get("id")
        if not org_id:
            raise VwcliError(f"Organization '{vault_name}' has no id")
        return str(org_id)

    def _nvm_bw_cmd(self, args: list[str]) -> list[str]:
        nvm_dir = os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))
        return [
            "bash",
            "-lc",
            f'. "{nvm_dir}/nvm.sh" && nvm exec --silent --lts -- bw "$@"',
            "_",
            *args,
        ]

    def _bw_cmd(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        capture: bool = True,
        stdin: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # stdin is only honoured when input_text is None (subprocess rejects both)
        effective_stdin = None if input_text is not None else stdin
        cmd = [self.bw_bin_path, *args] if self.bw_bin_path else self._nvm_bw_cmd(args)
        return subprocess.run(
            cmd,
            input=input_text,
            stdin=effective_stdin,
            text=True,
            capture_output=capture,
            check=False,
        )

    def bw_run(self, args: list[str], *, input_text: str | None = None) -> str:
        run = self._bw_cmd(args, input_text=input_text, capture=True)
        if run.returncode == 0:
            return run.stdout

        err_msg = run.stderr
        if BW_STALE_CIPHER_ERR in err_msg:
            print(
                "[info] Cipher state is stale, running bw sync and retrying once...",
                file=sys.stderr,
            )
            self._bw_cmd(["sync"], capture=True)
            run = self._bw_cmd(args, input_text=input_text, capture=True)
            if run.returncode == 0:
                return run.stdout
            err_msg = run.stderr

        if err_msg:
            print(err_msg, end="" if err_msg.endswith("\n") else "\n", file=sys.stderr)
        raise VwcliError("")

    def bw_run_interactive(self, args: list[str]) -> int:
        return self._bw_cmd(args, capture=False).returncode

    def _bw_run_json_list(self, args: list[str], *, input_text: str | None = None) -> list[dict[str, Any]]:
        data = json.loads(self.bw_run(args, input_text=input_text))
        if not isinstance(data, list):
            raise VwcliError(f"Unexpected bw {' '.join(args[:2])} output")
        return data

    def _bw_run_json_dict(self, args: list[str], *, input_text: str | None = None) -> dict[str, Any]:
        data = json.loads(self.bw_run(args, input_text=input_text))
        if not isinstance(data, dict):
            raise VwcliError(f"Unexpected bw {' '.join(args[:2])} output")
        return data

    def ensure_session(self) -> None:
        if not self.bw_session:
            raise VwcliError(f"BW_SESSION is not set. Run: {sys.argv[0]} login")

        if self._session_is_expired():
            print(
                "[info] BW_SESSION has expired, attempting silent re-unlock...",
                file=sys.stderr,
            )
            if not self._try_auto_renew():
                raise VwcliError(f"BW_SESSION expired and silent re-unlock failed. Run: {sys.argv[0]} login")
            print("[info] Session renewed successfully.", file=sys.stderr)

        os.environ["BW_SESSION"] = self.bw_session

    def _service_socket_url(self) -> str:
        sock = os.environ.get("BW_SERVE_SOCKET", f"/run/user/{os.getuid()}/bw.sock")
        return f"unix://{sock}"

    def _service_is_active(self) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "bitwarden-cli.service"],
            capture_output=True,
        )
        return result.returncode == 0

    def _service_start(self) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "start", "bitwarden-cli.service"],
            capture_output=True,
        )
        return result.returncode == 0

    def _wait_for_bw_serve(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._bw_serve_ping("/status"):
                return True
            time.sleep(0.1)
        return False

    def _maybe_use_systemd_service(self) -> bool:
        if not self._service_is_installed():
            return False

        if not self._service_is_active():
            print(
                "[info] bitwarden-cli.service is installed but not active; starting...",
                file=sys.stderr,
            )
            if not self._service_start():
                print("[warn] Failed to start bitwarden-cli.service", file=sys.stderr)
                return False
        else:
            print("[info] bitwarden-cli.service is already active", file=sys.stderr)

        self.bw_serve_url = self._service_socket_url()
        self.bw_serve_managed = False

        if self._wait_for_bw_serve(10.0):
            print("[info] bitwarden-cli.service is ready", file=sys.stderr)
            return True

        print(
            "[warn] bitwarden-cli.service did not become ready within 10s",
            file=sys.stderr,
        )
        self.bw_serve_url = ""
        return False

    def start_bw_serve(self) -> None:
        if self.bw_serve_url:
            if self._bw_serve_ping("/status"):
                self.bw_serve_managed = False
                return
            print(
                "[info] Configured BW_SERVE_URL is not reachable; checking bitwarden-cli.service...",
                file=sys.stderr,
            )
            if self._maybe_use_systemd_service():
                return
            self.bw_serve_url = ""

        if self._maybe_use_systemd_service():
            return

        self.ensure_session()
        port = self._find_free_port()
        self.bw_serve_url = f"http://{BW_SERVE_HOST}:{port}"

        with tempfile.NamedTemporaryFile(prefix="bw-serve-", delete=False) as tmp:
            self.bw_serve_log_path = tmp.name

        with open(self.bw_serve_log_path, "w", encoding="utf-8") as log_handle:
            self.bw_serve_proc = subprocess.Popen(
                self._bw_serve_command(BW_SERVE_HOST, str(port)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.bw_serve_managed = True

        for _ in range(BW_SERVE_STARTUP_RETRIES):
            if self._bw_serve_ping("/status") or self._bw_serve_ping("/health"):
                return
            if self.bw_serve_proc.poll() is not None:
                break
            time.sleep(BW_SERVE_STARTUP_DELAY)

        self.stop_bw_serve()
        raise VwcliError("Failed to start bw serve")

    def _bw_serve_command(self, host: str, port: str) -> list[str]:
        if self.bw_bin_path:
            return [self.bw_bin_path, "serve", "--hostname", host, "--port", port]
        return self._nvm_bw_cmd(["serve", "--hostname", host, "--port", port])

    def stop_bw_serve(self) -> None:
        if not self.bw_serve_managed:
            return

        if self.bw_serve_proc is not None and self.bw_serve_proc.poll() is None:
            self.bw_serve_proc.send_signal(signal.SIGTERM)
            try:
                self.bw_serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bw_serve_proc.kill()

        if self.bw_serve_log_path:
            with contextlib.suppress(OSError):
                Path(self.bw_serve_log_path).unlink()

        self.bw_serve_url = ""
        self.bw_serve_proc = None
        self.bw_serve_log_path = ""
        self.bw_serve_managed = False

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((BW_SERVE_HOST, 0))
            return s.getsockname()[1]

    def _unix_socket_path(self) -> str | None:
        parsed = urllib.parse.urlparse(self.bw_serve_url)
        return parsed.path if parsed.scheme == "unix" else None

    def _bw_serve_ping(self, path: str) -> bool:
        if not self.bw_serve_url:
            return False
        try:
            raw = self._bw_serve_raw_request("GET", path, timeout=1)
            return raw is not None
        except Exception:
            return False

    def _bw_serve_raw_request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 5,
    ) -> str:
        headers = headers or {}
        sock_path = self._unix_socket_path()
        if sock_path:
            conn = _UnixSocketHTTPConnection(sock_path, timeout=timeout)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.read().decode("utf-8")
            except (OSError, http.client.HTTPException) as exc:
                raise VwcliError(str(exc))
            finally:
                conn.close()
        else:
            req = urllib.request.Request(f"{self.bw_serve_url}{path}", data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                raise VwcliError(f"HTTP {exc.code}: {err_body}")
            except (urllib.error.URLError, TimeoutError) as exc:
                raise VwcliError(str(exc))

    def bw_serve_sync(self) -> None:
        """Ask the running bw serve process to sync its vault data from the server."""
        if not self.bw_serve_url:
            return
        with contextlib.suppress(VwcliError):
            self.bw_serve_request_json("POST", "/sync")

    def bw_serve_request_json(self, method: str, path: str, json_body: Any | None = None) -> dict[str, Any]:
        if not self.bw_serve_url:
            raise VwcliError("bw serve URL is not set")

        body = None
        headers: dict[str, str] = {}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        raw = self._bw_serve_raw_request(method, path, body=body, headers=headers)

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VwcliError(f"Invalid JSON from bw serve: {exc} (raw: {raw[:200]!r})")

    @staticmethod
    def bw_serve_response_success(response: dict[str, Any]) -> bool:
        return response.get("success") is True

    def bw_serve_try_json(
        self,
        warn_label: str,
        json_body: Any,
        specs: list[tuple[str, str]],
        *,
        has_cli_fallback: bool = True,
    ) -> Any:
        if not self.bw_serve_url:
            raise VwcliError("bw serve URL is not set")

        errors: list[str] = []
        for method, path in specs:
            try:
                response = self.bw_serve_request_json(method, path, json_body)
            except VwcliError as exc:
                errors.append(f"{method} {path}: {exc}")
                continue
            if self.bw_serve_response_success(response):
                return response.get("data")
            errors.append(f"{method} {path}: success=false message={response.get('message')}")

        if has_cli_fallback:
            print(
                f"[warn] bw serve {warn_label} endpoint failed, falling back to CLI.",
                file=sys.stderr,
            )
        else:
            print(f"[warn] bw serve {warn_label} endpoint failed.", file=sys.stderr)
        for msg in errors:
            print(f"[debug] {msg}", file=sys.stderr)
        raise VwcliError("fallback")

    @staticmethod
    def _exclude_trash(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in items if not item.get("deletedDate")]

    def _verify_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Re-fetch each item individually to get authoritative data including trash status.

        The bw serve list endpoint omits deletedDate even after sync; individual
        item gets return 404 for trashed items, making this the reliable check.
        """
        verified: list[dict[str, Any]] = []
        for item in items:
            item_id = item.get("id")
            if not item_id:
                continue
            try:
                fresh = self.bw_get_item(str(item_id))
                if not fresh.get("deletedDate"):
                    verified.append(fresh)
            except VwcliError:
                pass  # inaccessible or trashed
        return verified

    def bw_list_items_search(self, search: str, *, verify: bool = True) -> list[dict[str, Any]]:
        if UUID_RE.match(search):
            try:
                return self._exclude_trash([self.bw_get_item(search)])
            except VwcliError:
                return []

        if self.bw_serve_url:
            encoded_search = urllib.parse.quote(search, safe="")
            try:
                response = self.bw_serve_request_json("GET", f"/list/object/items?search={encoded_search}")
                data = response.get("data", {}).get("data")
                if self.bw_serve_response_success(response) and isinstance(data, list):
                    return self._verify_items(data) if verify else self._exclude_trash(data)
            except VwcliError:
                pass
            print(
                "[warn] bw serve search endpoint failed, falling back to CLI.",
                file=sys.stderr,
            )

        return self._exclude_trash(self._bw_run_json_list(["list", "items", "--search", search]))

    @staticmethod
    def _bw_serve_empty_list(response: dict[str, Any]) -> bool:
        return not response.get("success") and "not found" in (response.get("message") or "").lower()

    def _bw_serve_list(self, path: str, warn_label: str) -> list[dict[str, Any]] | None:
        """Fetch a list via bw serve. Returns the list, [] for not-found, or None on failure."""
        try:
            response = self.bw_serve_request_json("GET", path)
            if self.bw_serve_response_success(response):
                data = response.get("data", {}).get("data")
                return data if isinstance(data, list) else []
            if self._bw_serve_empty_list(response):
                return []
        except VwcliError:
            pass
        print(
            f"[warn] bw serve {warn_label} endpoint failed, falling back to CLI.",
            file=sys.stderr,
        )
        return None

    def bw_list_collections(self) -> list[dict[str, Any]]:
        if self.bw_serve_url:
            result = self._bw_serve_list("/list/object/collections", "collections")
            if result is not None:
                return result
        return self._bw_run_json_list(["list", "collections"])

    def bw_list_items_by_collection(self, collection_id: str) -> list[dict[str, Any]]:
        if self.bw_serve_url:
            encoded = urllib.parse.quote(collection_id, safe="")
            result = self._bw_serve_list(f"/list/object/items?collectionId={encoded}", "items by collection")
            if result is not None:
                return self._exclude_trash(result)

        return self._exclude_trash(self._bw_run_json_list(["list", "items", "--collectionid", collection_id]))

    def bw_list_folders(self) -> list[dict[str, Any]]:
        if self.bw_serve_url:
            result = self._bw_serve_list("/list/object/folders", "folders")
            if result is not None:
                return result

        return self._bw_run_json_list(["list", "folders"])

    def bw_get_item(self, item_id: str) -> dict[str, Any]:
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json("GET", f"/object/item/{item_id}")
                data = response.get("data")
                if self.bw_serve_response_success(response) and isinstance(data, dict):
                    return data
            except VwcliError:
                print(
                    "[warn] bw serve get item endpoint failed, falling back to CLI.",
                    file=sys.stderr,
                )

        return self._bw_run_json_dict(["get", "item", item_id])

    def bw_get_template_item(self) -> dict[str, Any]:
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json("GET", "/object/template/item")
                if self.bw_serve_response_success(response):
                    data = response.get("data")
                    if isinstance(data, dict) and isinstance(data.get("template"), dict):
                        return data["template"]
                    if isinstance(data, dict):
                        return data
            except VwcliError:
                pass

        return self._bw_run_json_dict(["get", "template", "item"])

    def bw_encode_json(self, payload: Any) -> str:
        return self.bw_run(["encode"], input_text=json.dumps(payload, separators=(",", ":"))).strip()

    def bw_create_item(self, item_json: dict[str, Any]) -> dict[str, Any]:
        if self.bw_serve_url:
            try:
                data = self.bw_serve_try_json("create", item_json, [("POST", "/object/item")])
                if isinstance(data, dict):
                    return data
            except VwcliError:
                pass

        return self._bw_run_json_dict(["create", "item"], input_text=self.bw_encode_json(item_json))

    def bw_edit_item(self, item_id: str, item_json: dict[str, Any]) -> None:
        if self.bw_serve_url:
            try:
                self.bw_serve_try_json(
                    "edit",
                    item_json,
                    [
                        ("PUT", f"/object/item/{item_id}"),
                        ("POST", f"/object/item/{item_id}"),
                    ],
                )
                return
            except VwcliError:
                pass

        self.bw_run(["edit", "item", item_id], input_text=self.bw_encode_json(item_json))

    def bw_move_item_to_org(self, item_id: str, org_id: str, collection_ids: list[str]) -> None:
        if self.bw_serve_url:
            try:
                self.bw_serve_try_json("move", collection_ids, [("POST", f"/move/{item_id}/{org_id}")])
                return
            except VwcliError:
                pass

        self.bw_run(["move", item_id, org_id], input_text=self.bw_encode_json(collection_ids))

    def bw_create_attachment(self, item_id: str, file_path: str) -> dict[str, Any]:
        return self._bw_run_json_dict(["create", "attachment", "--file", file_path, "--itemid", item_id])

    def bw_delete_item(self, item_id: str) -> None:
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json("DELETE", f"/object/item/{item_id}")
                if self.bw_serve_response_success(response):
                    return
            except VwcliError:
                pass
        self.bw_run(["delete", "item", item_id])

    def bw_delete_attachment(self, item_id: str, attachment_id: str) -> None:
        self.bw_run(["delete", "attachment", attachment_id, "--itemid", item_id])

    def bw_set_item_collections(self, item_id: str, org_id: str, collection_ids: list[str]) -> None:
        if self.bw_serve_url:
            try:
                self.bw_serve_try_json(
                    "item-collections",
                    collection_ids,
                    [
                        (
                            "PUT",
                            f"/object/item-collections/{item_id}?organizationid={org_id}",
                        )
                    ],
                )
                return
            except VwcliError:
                pass

        self.bw_run(
            ["edit", "item-collections", item_id, "--organizationid", org_id],
            input_text=self.bw_encode_json(collection_ids),
        )

    def bw_get_collection(self, collection_id: str) -> dict[str, Any]:
        return self.bw_serve_request_json("GET", f"/object/collection/{collection_id}")

    def bw_create_collection(self, org_id: str, name: str, parent_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "organizationId": org_id,
            "name": name,
            "externalId": None,
            "groups": [],
        }
        if parent_id:
            payload["parentId"] = parent_id
        data = self.bw_serve_try_json(
            "create-collection",
            payload,
            [("POST", f"/object/org-collection?organizationid={org_id}")],
            has_cli_fallback=False,
        )
        if isinstance(data, dict):
            return data
        raise VwcliError("create-collection returned unexpected data")

    def bw_update_collection(self, collection_id: str, org_id: str, name: str, parent_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "organizationId": org_id,
            "name": name,
            "externalId": None,
            "groups": [],
        }
        if parent_id:
            payload["parentId"] = parent_id
        data = self.bw_serve_try_json(
            "update-collection",
            payload,
            [
                (
                    "PUT",
                    f"/object/org-collection/{collection_id}?organizationid={org_id}",
                )
            ],
            has_cli_fallback=False,
        )
        if isinstance(data, dict):
            return data
        raise VwcliError("update-collection returned unexpected data")

    def bw_delete_collection(self, collection_id: str, org_id: str) -> None:
        self.bw_serve_try_json(
            "delete-collection",
            None,
            [
                (
                    "DELETE",
                    f"/object/org-collection/{collection_id}?organizationid={org_id}",
                )
            ],
            has_cli_fallback=False,
        )

    @staticmethod
    def _service_env_file() -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(xdg) / "systemd" / "user" / "bitwarden-cli.env"

    @staticmethod
    def _service_is_installed() -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "cat", "bitwarden-cli.service"],
            capture_output=True,
        )
        return result.returncode == 0

    @staticmethod
    def _service_restart() -> None:
        subprocess.run(["systemctl", "--user", "restart", "bitwarden-cli.service"], check=False)

    def _update_service_env(self, session: str, expires: int) -> bool:
        """Update BW_SESSION (and BW_SESSION_EXPIRES) in the systemd env file.

        Returns True if the file was written.
        """
        env_path = self._service_env_file()
        if not env_path.exists():
            return False

        lines = env_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        found_session = False
        found_expires = False
        for line in lines:
            if line.startswith("BW_SESSION="):
                out.append(f"BW_SESSION={session}")
                found_session = True
            elif line.startswith("BW_SESSION_EXPIRES="):
                out.append(f"BW_SESSION_EXPIRES={expires}")
                found_expires = True
            else:
                out.append(line)
        if not found_session:
            out.append(f"BW_SESSION={session}")
        if not found_expires:
            out.append(f"BW_SESSION_EXPIRES={expires}")

        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        safe_chmod(env_path, 0o600)
        return True

    def _apply_new_session(self, session: str, *, restart_service: bool = True, quiet: bool = False) -> None:
        """Store a fresh session token everywhere it needs to live."""
        expires = int(time.time()) + BW_SESSION_DEFAULT_TTL
        self.bw_session = session
        self.bw_session_expires = expires
        os.environ["BW_SESSION"] = session

        self.config.set("BW_SESSION", session)
        self.config.set("BW_SESSION_EXPIRES", str(expires))
        if not quiet:
            print(f"BW_SESSION cached in: {self.config.config_file}")

        if self._service_is_installed():
            updated = self._update_service_env(session, expires)
            if updated and restart_service:
                self._service_restart()
                if not quiet:
                    print("bitwarden-cli.service restarted with new token")

    def _session_is_expired(self) -> bool:
        if not self.bw_session_expires:
            return False  # no expiry info stored → assume still valid
        return time.time() >= self.bw_session_expires

    def _try_auto_renew(self) -> bool:
        """Silently re-unlock and propagate a fresh token. Returns True on success.

        Uses DEVNULL for stdin so bw cannot prompt for a password — if bw requires
        interactive input the call fails immediately and we return False.
        """
        run = self._bw_cmd(["unlock", "--raw"], stdin=subprocess.DEVNULL)
        if run.returncode != 0:
            return False
        session = run.stdout.strip()
        if not session:
            return False
        self._apply_new_session(session, restart_service=True, quiet=True)
        return True

    def cmd_login(self, _ns: argparse.Namespace) -> None:
        self.config.ensure_dir()

        if self._bw_cmd(["login", "--check"]).returncode != 0:
            print("[info] No active bw login, starting interactive login...")
            if self.bw_run_interactive(["login"]) != 0:
                raise VwcliError("bw login failed")

        session = self.bw_run(["unlock", "--raw"]).strip()
        if not session:
            raise VwcliError("Failed to obtain BW_SESSION from bw unlock")

        self._apply_new_session(session, restart_service=True)

    def bw_sync(self) -> None:
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json("POST", "/sync")
                if self.bw_serve_response_success(response):
                    return
            except VwcliError:
                pass
        self.bw_run(["sync"])

    @staticmethod
    def _compute_collection_full_paths(
        collections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compute full hierarchical names (Parent/Child/Leaf) from parentId chains."""
        by_id = {str(c.get("id")): c for c in collections if c.get("id")}
        result: list[dict[str, Any]] = []
        for col in collections:
            col_id = str(col.get("id") or "")
            name = str(col.get("name") or "")
            parent_id = str(col.get("parentId") or "") if col.get("parentId") else ""
            parts = [name]
            current_id = parent_id
            seen: set[str] = {col_id}
            while current_id and current_id in by_id and current_id not in seen:
                seen.add(current_id)
                parent = by_id[current_id]
                parts.insert(0, str(parent.get("name") or ""))
                current_id = str(parent.get("parentId") or "") if parent.get("parentId") else ""
            full_name = "/".join(parts)
            result.append({
                "id": col_id,
                "name": full_name,
                "organizationId": col.get("organizationId"),
                "parentId": parent_id or None,
            })
        return result

    def refresh_collections_cache(self) -> None:
        self.ensure_session()
        self.config.ensure_cache_dir()
        self.bw_sync()

        data = self.bw_list_collections()
        normalized = self._compute_collection_full_paths(data)
        self.config.collection_cache.write_text(json.dumps(normalized, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(f"Collection cache written to: {self.config.collection_cache}")

    def resolve_collection_by_name(self, col_name: str) -> dict[str, Any]:
        if not self.config.collection_cache.exists():
            raise VwcliError(f"Collection cache not found. Run: {sys.argv[0]} collections cache")

        data = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise VwcliError("Collection cache is invalid")

        matches = [item for item in data if item.get("name") == col_name]
        if len(matches) == 0:
            raise VwcliError(f"No collection named '{col_name}' in cache. Run '{sys.argv[0]} collections cache' or check spelling.")
        if len(matches) > 1:
            print(f"Ambiguous collection name '{col_name}'. Matches:", file=sys.stderr)
            for m in matches:
                print(
                    f"  - {m.get('name')} [id={m.get('id')}] org={m.get('organizationId')}",
                    file=sys.stderr,
                )
            raise VwcliError("Collection name must be unique.")
        return matches[0]

    def find_item_id_by_search(self, search: str) -> str:
        self.ensure_session()
        items = self.bw_list_items_search(search)

        if len(items) == 0:
            raise VwcliError(f"No items matched search '{search}'")
        if len(items) > 1:
            print(f"Search '{search}' matched multiple items:", file=sys.stderr)
            for item in items:
                print(
                    f"  - {item.get('name', '')} [{item.get('id', '')}]",
                    file=sys.stderr,
                )
            raise VwcliError("Please use --id or a more specific --search")

        item_id = items[0].get("id")
        if not item_id:
            raise VwcliError("Matched item has no id")
        return str(item_id)

    def _resolve_item_id(self, ns: argparse.Namespace) -> str:
        return ns.id or self.find_item_id_by_search(ns.search)

    @staticmethod
    def apply_item_updates(
        item_json: dict[str, Any],
        name: str,
        username: str,
        password: str,
        notes: str,
        uris: list[str],
        add_uris: list[str] | None = None,
        remove_uris: list[str] | None = None,
    ) -> dict[str, Any]:
        out = copy.deepcopy(item_json)
        out["type"] = 1

        if name:
            out["name"] = name
        if notes:
            out["notes"] = notes

        login = out.get("login")
        if not isinstance(login, dict):
            login = {}
            out["login"] = login

        if username:
            login["username"] = username
        if password:
            login["password"] = password
        if uris:
            login["uris"] = [{"uri": url, "match": match} for url, match in (parse_uri(u) for u in uris)]
        elif add_uris or remove_uris:
            existing = list(login.get("uris") or [])
            if remove_uris:
                remove_set = set(remove_uris)
                existing = [u for u in existing if u.get("uri") not in remove_set]
            if add_uris:
                existing += [{"uri": url, "match": match} for url, match in (parse_uri(u) for u in add_uris)]
            login["uris"] = existing

        return out

    @staticmethod
    def set_organization_id(item_json: dict[str, Any], organization_id: str) -> dict[str, Any]:
        return {**item_json, "organizationId": organization_id}

    @staticmethod
    def build_clone_payload(item_json: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item_json.items() if k not in _CLONE_STRIP_KEYS}

    def assign_collection_to_item(self, item_id: str, collection_name: str) -> None:
        col_json = self.resolve_collection_by_name(collection_name)
        collection_id = col_json.get("id")
        org_id = col_json.get("organizationId")
        if not org_id:
            raise VwcliError(f"Collection '{collection_name}' has no organizationId; cannot assign.")

        item_json = self.bw_get_item(item_id)
        item_org = item_json.get("organizationId")
        collection_ids = [str(collection_id)]

        if item_org in (None, "null"):
            print("[info] Moving item into organization and assigning collection...")
            self.bw_move_item_to_org(item_id, str(org_id), collection_ids)
        else:
            if str(item_org) != str(org_id):
                raise VwcliError(f"Item belongs to organization {item_org} but collection belongs to {org_id}")
            print("[info] Updating item collection assignments...")
            self.bw_set_item_collections(item_id, str(org_id), collection_ids)

    def ansible_vault_encrypt(self, password: str, var_name: str = "password") -> None:
        if not password:
            raise VwcliError("No password to encrypt")
        if not shutil.which("ansible-vault"):
            raise VwcliError("Missing required command: ansible-vault")
        stderr_messages: list[str] = []
        for subcommand in ("encrypt-string", "encrypt_string"):
            run = subprocess.run(
                ["ansible-vault", subcommand, "--stdin-name", var_name],
                input=password,
                text=True,
                capture_output=True,
                check=False,
            )
            if run.returncode == 0:
                print(run.stdout, end="" if run.stdout.endswith("\n") else "\n")
                return
            if run.stderr:
                stderr_messages.append(run.stderr.strip())
        raise VwcliError(stderr_messages[-1] if stderr_messages else "ansible-vault encrypt-string failed")

    def bw_generate_password(self) -> str:
        gen_args = [
            "--length",
            "24",
            "--uppercase",
            "--lowercase",
            "--number",
            "--special",
        ]
        if self.bw_serve_url:
            try:
                response = self.bw_serve_request_json(
                    "GET",
                    "/generate?length=24&uppercase=true&lowercase=true&number=true&special=true",
                )
                if self.bw_serve_response_success(response):
                    data = response.get("data")
                    pw = data.get("data") if isinstance(data, dict) else data
                    if isinstance(pw, str) and pw:
                        return pw
            except VwcliError:
                print(
                    "[warn] bw serve generate endpoint failed, falling back to CLI.",
                    file=sys.stderr,
                )

        return self.bw_run(["generate", *gen_args]).strip()

    def cmd_create(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        if ns.refresh_cache:
            self.refresh_collections_cache()

        password = ns.password
        if ns.generate_password:
            password = self.bw_generate_password()

        item_json = self.bw_get_template_item()
        item_json = self.apply_item_updates(item_json, ns.name, ns.username, password, ns.notes or "", ns.uris)

        if ns.notes is None:
            item_json["notes"] = None

        if ns.organization_id:
            item_json = self.set_organization_id(item_json, ns.organization_id)

        if ns.vault:
            org_id = self.resolve_vault_to_org_id(ns.vault)
            item_json = self.set_organization_id(item_json, org_id)

        if ns.dry_run:
            print(json.dumps(item_json, indent=2))
            return

        created_json = self.bw_create_item(item_json)
        item_id = created_json.get("id")
        if not item_id:
            raise VwcliError("Create succeeded but item ID was not returned")

        self.bw_serve_sync()
        print(f"Created item: {created_json.get('name', '')} [{item_id}]")
        if ns.generate_password:
            print(f"Generated password: {password}")

        if ns.collection:
            self.assign_collection_to_item(str(item_id), ns.collection)
            self.bw_serve_sync()
            print(f"Assigned collection: {ns.collection}")

        if ns.to_ansible_vault:
            self.ansible_vault_encrypt(str(password or ""))

    def _prepare_update_target(
        self,
        ns: argparse.Namespace,
        current_json: dict[str, Any],
        vault_org_id: str,
    ) -> tuple[str, dict[str, Any]]:
        """Return the target ID and JSON for an update or clone operation."""
        if not ns.clone:
            return str(current_json.get("id") or ""), copy.deepcopy(current_json)

        clone_payload = self.build_clone_payload(current_json)
        if ns.name:
            clone_payload["name"] = ns.name
        if ns.organization_id:
            clone_payload = self.set_organization_id(clone_payload, ns.organization_id)
        if vault_org_id:
            clone_payload = self.set_organization_id(clone_payload, vault_org_id)

        target_org_id = str(vault_org_id or ns.organization_id or "")
        source_org_id = str(current_json.get("organizationId") or "")
        if target_org_id and target_org_id != source_org_id:
            # Collection and folder IDs are scoped to a single vault, so
            # the source item's values are invalid for the target vault.
            # Rebuild the collection assignment for the target vault.
            clone_payload.pop("folderId", None)
            if ns.collection:
                col_json = self.resolve_collection_by_name(ns.collection)
                col_id = col_json.get("id")
                if not col_id:
                    raise VwcliError(f"Collection '{ns.collection}' has no id; cannot assign.")
                col_org = str(col_json.get("organizationId") or "")
                if col_org and vault_org_id and col_org != str(vault_org_id):
                    raise VwcliError(f"Vault '{ns.vault}' (org {vault_org_id}) and collection '{ns.collection}' (org {col_org}) are in different organizations")
                if col_org:
                    clone_payload = self.set_organization_id(clone_payload, col_org)
                    target_org_id = col_org
                clone_payload["collectionIds"] = [str(col_id)]
            else:
                clone_payload.pop("collectionIds", None)

        if ns.dry_run:
            return "<clone-id>", clone_payload

        created_clone = self.bw_create_item(clone_payload)
        clone_id = created_clone.get("id")
        if not clone_id:
            raise VwcliError("Clone creation succeeded but clone ID was not returned")
        source_id = current_json.get("id") or ""
        print(f"Cloned item [{source_id}] -> [{clone_id}]")
        return str(clone_id), created_clone

    def _apply_update_fields(
        self,
        target_json: dict[str, Any],
        ns: argparse.Namespace,
        password: str,
        vault_org_id: str,
    ) -> dict[str, Any]:
        """Apply name, username, password, notes, URIs and organization overrides."""
        if ns.name or ns.username or password or ns.notes or ns.uris or ns.add_uris or ns.remove_uris:
            target_json = self.apply_item_updates(
                target_json,
                ns.name,
                ns.username,
                password,
                ns.notes,
                ns.uris,
                ns.add_uris,
                ns.remove_uris,
            )

        if ns.organization_id:
            target_json = self.set_organization_id(target_json, ns.organization_id)

        if vault_org_id:
            target_json = self.set_organization_id(target_json, vault_org_id)

        return target_json

    def cmd_update(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        if ns.refresh_cache:
            self.refresh_collections_cache()

        password = ns.password
        if ns.generate_password:
            password = self.bw_generate_password()

        item_id = ns.id or self.find_item_id_by_search(ns.search)
        current_json = self.bw_get_item(item_id)
        vault_org_id = self.resolve_vault_to_org_id(ns.vault) if ns.vault else ""

        target_id, target_json = self._prepare_update_target(ns, current_json, vault_org_id)
        target_json = self._apply_update_fields(target_json, ns, password, vault_org_id)

        if ns.dry_run:
            print(json.dumps(target_json, indent=2))
            return

        self.bw_edit_item(target_id, target_json)
        self.bw_serve_sync()
        print(f"Updated item [{target_id}]")
        if ns.generate_password:
            print(f"Generated password: {password}")

        if ns.collection:
            self.assign_collection_to_item(target_id, ns.collection)
            self.bw_serve_sync()
            print(f"Assigned collection: {ns.collection}")

        if ns.to_ansible_vault:
            final_password = str((target_json.get("login") or {}).get("password") or "")
            self.ansible_vault_encrypt(final_password)

    def _build_org_map(self) -> dict[str, str]:
        try:
            orgs = self.bw_list_organizations()
            return {str(o.get("id") or ""): str(o.get("name") or "") for o in orgs if o.get("id")}
        except VwcliError:
            print(
                "[warn] Could not list organizations; vault column will show 'Unknown'.",
                file=sys.stderr,
            )
            return {}

    def _build_folder_map(self) -> dict[str, str]:
        folders = self.bw_list_folders()
        return {str(f.get("id") or ""): str(f.get("name") or "") for f in folders if f.get("id")}

    def _build_collection_map(self, required_ids: set[str]) -> dict[str, str]:
        if not self.config.collection_cache.exists():
            return {}
        try:
            cached = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
            if not isinstance(cached, list):
                return {}
            col_map = {str(c.get("id") or ""): str(c.get("name") or "") for c in cached}
            if required_ids and not all(cid in col_map for cid in required_ids):
                self.refresh_collections_cache()
                refreshed = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
                if isinstance(refreshed, list):
                    col_map = {str(c.get("id") or ""): str(c.get("name") or "") for c in refreshed}
            return col_map
        except json.JSONDecodeError:
            print(
                f"[warn] Invalid collection cache at {self.config.collection_cache}, ignoring cache values.",
                file=sys.stderr,
            )
            return {}

    def _fetch_search_items(self, ns: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
        if not ns.query and not ns.uri_filter:
            raise VwcliError("Provide a QUERY and/or --uri to search")

        search = " ".join(ns.query)
        items = self.bw_list_items_search(search, verify=False)

        if ns.uri_filter:
            uri_lower = ns.uri_filter.lower()
            items = [item for item in items if any(uri_lower in (u.get("uri") or "").lower() for u in (item.get("login") or {}).get("uris") or [])]

        return search, items

    def _prepare_search_display_items(
        self,
        items: list[dict[str, Any]],
        ns: argparse.Namespace,
        folder_map: dict[str, str],
        col_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        sorted_items = sorted(
            items,
            key=lambda item: (
                item_group_label(item, folder_map, col_map),
                str(item.get("name") or ""),
            ),
        )
        # Verify only the items that will actually be displayed (avoids N+1 for large result sets)
        return self._verify_items(sorted_items[: ns.limit])

    def cmd_search(self, ns: argparse.Namespace) -> None:
        self.ensure_session()

        search, items = self._fetch_search_items(ns)

        if ns.to_ansible_vault:
            first_item = self._verify_items(items[:1])
            if not first_item:
                raise VwcliError(f"No items matched search '{search}'")
            login = first_item[0].get("login") or {}
            self.ansible_vault_encrypt(str(login.get("password") or ""))
            return

        org_map = self._build_org_map()
        folder_map = self._build_folder_map()
        item_col_ids = {str(cid or "") for item in items for cid in (item.get("collectionIds") or [])}
        col_map = self._build_collection_map(item_col_ids)

        display_items = self._prepare_search_display_items(items, ns, folder_map, col_map)
        render_search_results(
            display_items,
            output_json=ns.output_json,
            org_map=org_map,
            folder_map=folder_map,
            col_map=col_map,
        )

    def with_bw_serve(self, func: Callable[[argparse.Namespace], None], ns: argparse.Namespace) -> None:
        self.start_bw_serve()
        try:
            func(ns)
        finally:
            self.stop_bw_serve()

    def cmd_attachment(self, ns: argparse.Namespace) -> None:
        sub = getattr(ns, "attachment_command", None)
        if sub in ("list", None):
            if not ns.id and not ns.search:
                print(
                    "Usage: vwcli attachment [list] (--id VALUE | --search VALUE) [--json]",
                    file=sys.stderr,
                )
                return
            self.cmd_attachment_show(ns)
        elif sub == "add":
            self.cmd_attachment_add(ns)
        elif sub == "delete":
            self.cmd_attachment_delete(ns)

    def cmd_attachment_show(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        item_id = self._resolve_item_id(ns)
        item = self.bw_get_item(item_id)
        attachments = item.get("attachments") or []

        if ns.output_json:
            print(json.dumps(attachments, indent=2))
            return

        if not attachments:
            print(f"No attachments on item [{item_id}]")
            return

        if sys.stdout.isatty():
            table = Table(title=f"Attachments for '{item.get('name', '')}' [{item_id}]")
            table.add_column("ID", style="dim", no_wrap=True)
            table.add_column("File Name", style="bold")
            table.add_column("Size", justify="right")
            for att in attachments:
                table.add_row(att.get("id", ""), att.get("fileName", ""), att.get("size", ""))
            Console().print(table)
        else:
            print("id\tfileName\tsize")
            for att in attachments:
                print(f"{att.get('id', '')}\t{att.get('fileName', '')}\t{att.get('size', '')}")

    def cmd_attachment_add(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        item_id = self._resolve_item_id(ns)
        file_path = ns.file
        if not Path(file_path).exists():
            raise VwcliError(f"File not found: {file_path}")
        result = self.bw_create_attachment(item_id, file_path)
        att_list = result.get("attachments") or []
        added = att_list[-1] if att_list else {}
        print(f"Attachment added to item [{item_id}]: {added.get('fileName', file_path)} [{added.get('id', '')}]")

    def cmd_attachment_delete(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        item_id = self._resolve_item_id(ns)
        self.bw_delete_attachment(item_id, ns.attachment_id)
        print(f"Attachment [{ns.attachment_id}] deleted from item [{item_id}]")

    def cmd_collections(self, ns: argparse.Namespace) -> None:
        sub = getattr(ns, "collections_command", "")
        if sub == "cache":
            self.refresh_collections_cache()
        elif sub == "list":
            self.cmd_collections_list(ns)
        elif sub == "add":
            self.cmd_collections_add(ns)
        elif sub == "update":
            self.cmd_collections_update(ns)
        elif sub == "delete":
            self.cmd_collections_delete(ns)
        elif sub == "move":
            self.cmd_collections_move(ns)
        elif sub == "search":
            self.cmd_collections_search(ns)
        else:
            print(
                "Usage: vwcli collections {cache|list|add|update|delete|move|search}",
                file=sys.stderr,
            )

    def cmd_collections_list(self, ns: argparse.Namespace) -> None:
        if ns.refresh_cache:
            self.refresh_collections_cache()
        data = self.bw_list_collections()
        if not data:
            print("No collections found.")
            return
        if sys.stdout.isatty():
            table = Table(title="Collections", show_lines=False, highlight=True)
            table.add_column("ID", style="dim")
            table.add_column("Name", style="bold")
            table.add_column("Organization ID", style="dim")
            for item in data:
                table.add_row(
                    str(item.get("id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("organizationId") or ""),
                )
            Console().print(table)
        else:
            for item in data:
                print(
                    "\t".join([
                        str(item.get("id") or ""),
                        str(item.get("name") or ""),
                        str(item.get("organizationId") or ""),
                    ])
                )

    def cmd_collections_search(self, ns: argparse.Namespace) -> None:
        if not ns.query:
            raise VwcliError("QUERY is required")
        query = " ".join(ns.query).lower()

        if not self.config.collection_cache.exists():
            raise VwcliError(f"Collection cache not found. Run: {sys.argv[0]} collections cache")

        data = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise VwcliError("Collection cache is invalid")

        matches = [item for item in data if query in str(item.get("name") or "").lower()]
        if not matches:
            print("No collections matched the query.")
            return

        if sys.stdout.isatty():
            table = Table(title=f"Collections: {len(matches)}", show_lines=False, highlight=True)
            table.add_column("ID", style="dim")
            table.add_column("Name", style="bold")
            table.add_column("Organization ID", style="dim")
            for item in matches:
                table.add_row(
                    str(item.get("id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("organizationId") or ""),
                )
            Console().print(table)
        else:
            for item in matches:
                print(
                    "\t".join([
                        str(item.get("id") or ""),
                        str(item.get("name") or ""),
                        str(item.get("organizationId") or ""),
                    ])
                )

    def _get_collection_full_path(self, collection_id: str) -> str:
        """Return the full path of a collection from cache or API."""
        if self.config.collection_cache.exists():
            try:
                cached = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    for item in cached:
                        if str(item.get("id") or "") == collection_id:
                            return str(item.get("name") or "")
            except (json.JSONDecodeError, OSError):
                pass
        try:
            all_cols = self.bw_list_collections()
            normalized = self._compute_collection_full_paths(all_cols)
            for item in normalized:
                if str(item.get("id") or "") == collection_id:
                    return str(item.get("name") or "")
        except VwcliError:
            pass
        try:
            resp = self.bw_get_collection(collection_id)
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            return str(data.get("name") or "")
        except VwcliError:
            return ""

    def cmd_collections_add(self, ns: argparse.Namespace) -> None:
        name = ns.name
        org_id = ns.organization_id
        parent_id = ns.parent_id or ""

        if not name:
            raise VwcliError("--name is required")

        if parent_id:
            parent_resp = self.bw_get_collection(parent_id)
            parent_data = parent_resp.get("data", {}) if isinstance(parent_resp, dict) else {}
            parent_org = str(parent_data.get("organizationId") or "") if isinstance(parent_data, dict) else ""
            if not org_id:
                if not parent_org:
                    raise VwcliError(f"Parent collection {parent_id} has no organizationId")
                org_id = parent_org

            if "/" not in name:
                parent_path = self._get_collection_full_path(parent_id)
                if parent_path:
                    name = f"{parent_path}/{name}"
        elif not org_id:
            raise VwcliError("--organization-id is required when no --parent-id is given")

        result = self.bw_create_collection(org_id, name, parent_id)
        col_id = result.get("id") if isinstance(result, dict) else None
        print(f"Created collection '{name}' [{col_id}]")

        if self.config.collection_cache.exists() and col_id:
            try:
                cached = json.loads(self.config.collection_cache.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    cached.append({
                        "id": str(col_id),
                        "name": name,
                        "organizationId": org_id,
                        "parentId": parent_id or None,
                    })
                    self.config.collection_cache.write_text(
                        json.dumps(cached, ensure_ascii=True, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (json.JSONDecodeError, OSError):
                pass

    def cmd_collections_update(self, ns: argparse.Namespace) -> None:
        if not ns.id or not ns.name:
            raise VwcliError("--id and --name are required")
        current = self.bw_get_collection(ns.id)
        data = current.get("data", {}) if isinstance(current, dict) else {}
        org_id = str(data.get("organizationId") or "") if isinstance(data, dict) else ""
        if not org_id:
            raise VwcliError(f"Collection {ns.id} has no organizationId")
        result = self.bw_update_collection(ns.id, org_id, ns.name, ns.parent_id or "")
        col_id = result.get("id") if isinstance(result, dict) else ns.id
        print(f"Updated collection [{col_id}] name='{ns.name}'")

        if self.config.collection_cache.exists():
            with contextlib.suppress(VwcliError):
                self.refresh_collections_cache()

    def cmd_collections_delete(self, ns: argparse.Namespace) -> None:
        if not ns.id:
            raise VwcliError("--id is required")
        if not ns.yes:
            current = self.bw_get_collection(ns.id)
            data = current.get("data", {}) if isinstance(current, dict) else {}
            name = str(data.get("name") or "") if isinstance(data, dict) else ""
            ans = input(f"Delete collection '{name}' [{ns.id}]? [y/N] ")
            if ans.strip().lower() != "y":
                print("Aborted.")
                return
        self.bw_delete_collection(ns.id, ns.organization_id or "")
        print(f"Deleted collection [{ns.id}]")

    def cmd_collections_move(self, ns: argparse.Namespace) -> None:
        if not ns.id or not ns.to_parent_id:
            raise VwcliError("--id and --to-parent-id are required")
        current = self.bw_get_collection(ns.id)
        data = current.get("data", {}) if isinstance(current, dict) else {}
        if not isinstance(data, dict):
            raise VwcliError(f"Failed to fetch collection {ns.id}")
        org_id = str(data.get("organizationId") or "")
        name = str(data.get("name") or "")
        if not org_id:
            raise VwcliError(f"Collection {ns.id} has no organizationId")

        try:
            self.bw_update_collection(ns.id, org_id, name, ns.to_parent_id)
            print(f"Moved collection [{ns.id}] to parent [{ns.to_parent_id}]")
            return
        except VwcliError:
            pass

        print("[info] Direct parent update not supported, using bulk fallback...")
        try:
            new_col = self.bw_create_collection(org_id, name, ns.to_parent_id)
            new_id = str(new_col.get("id") or "") if isinstance(new_col, dict) else ""
            if not new_id:
                raise VwcliError("Failed to create target collection")
        except VwcliError:
            print("[info] Bulk fallback requires creating new collection.")
            raise

        items = self.bw_list_items_by_collection(ns.id)
        if not items:
            print("[info] No items in source collection.")
        else:
            moved: list[tuple[str, str]] = []  # (item_id, item_name) of successfully moved items
            errors = 0
            for item in items:
                item_id = str(item.get("id") or "")
                item_name = str(item.get("name") or "")
                try:
                    self.bw_set_item_collections(item_id, org_id, [new_id])
                    moved.append((item_id, item_name))
                    print(f"  Moved item: {item_name} [{item_id}]")
                except VwcliError as e:
                    print(f"  [error] {item_name} [{item_id}]: {e}", file=sys.stderr)
                    errors += 1
            if errors:
                print(
                    f"[info] Rolling back {len(moved)} already-moved item(s)...",
                    file=sys.stderr,
                )
                for item_id, item_name in moved:
                    try:
                        self.bw_set_item_collections(item_id, org_id, [ns.id])
                    except VwcliError as e:
                        print(
                            f"  [error] rollback failed for {item_name} [{item_id}]: {e}",
                            file=sys.stderr,
                        )
                try:
                    self.bw_delete_collection(new_id, org_id)
                except VwcliError:
                    print(
                        f"[warn] Could not delete partially-created collection [{new_id}].",
                        file=sys.stderr,
                    )
                raise VwcliError(f"{errors} item(s) failed to move. Rolled back; original collection [{ns.id}] is intact.")

        self.bw_delete_collection(ns.id, org_id)
        print(f"Moved collection '{name}' [{ns.id}] -> [{new_id}] (under parent [{ns.to_parent_id}])")

    def cmd_delete(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        item_id = self._resolve_item_id(ns)
        item = self.bw_get_item(item_id)
        if not ns.yes:
            ans = input(f"Delete '{item.get('name', '')}' [{item_id}]? [y/N] ")
            if ans.strip().lower() != "y":
                print("Aborted.")
                return
        self.bw_delete_item(item_id)
        self.bw_serve_sync()
        print(f"Deleted item [{item_id}]")

    def cmd_move(self, ns: argparse.Namespace) -> None:
        self.ensure_session()
        if ns.refresh_cache:
            self.refresh_collections_cache()

        src_col = self.resolve_collection_by_name(ns.from_collection)
        dst_col = self.resolve_collection_by_name(ns.to_collection)

        src_id = str(src_col.get("id") or "")
        dst_id = str(dst_col.get("id") or "")
        src_org = str(src_col.get("organizationId") or "")
        dst_org = str(dst_col.get("organizationId") or "")

        if not src_org:
            raise VwcliError(f"Source collection '{ns.from_collection}' has no organizationId")
        if not dst_org:
            raise VwcliError(f"Target collection '{ns.to_collection}' has no organizationId")
        if src_org != dst_org:
            raise VwcliError(f"Collections belong to different organizations ({src_org} vs {dst_org})")

        items = self.bw_list_items_by_collection(src_id)

        if ns.search:
            search_lower = ns.search.lower()
            items = [i for i in items if search_lower in (i.get("name") or "").lower()]

        if not items:
            print("[info] No items found in source collection matching the criteria.")
            return

        print(f"Items to move from '{ns.from_collection}' → '{ns.to_collection}':")
        for item in items:
            print(f"  - {item.get('name', '')} [{item.get('id', '')}]")

        if ns.dry_run:
            print(f"[dry-run] Would move {len(items)} item(s). No changes made.")
            return

        if not ns.yes:
            answer = input(f"\nMove {len(items)} item(s)? [y/N] ").strip().lower()
            if answer != "y":
                print("Aborted.")
                return

        errors = 0
        for item in items:
            item_id = str(item.get("id") or "")
            name = str(item.get("name") or "")
            try:
                self.bw_set_item_collections(item_id, dst_org, [dst_id])
                print(f"  Moved: {name} [{item_id}]")
            except VwcliError as e:
                print(f"  [error] {name} [{item_id}]: {e}", file=sys.stderr)
                errors += 1

        if errors:
            raise VwcliError(f"{errors} item(s) failed to move.")
        self.bw_serve_sync()
        print(f"Done. Moved {len(items)} item(s) to '{ns.to_collection}'.")

    def cmd_cache_collections(self, _ns: argparse.Namespace) -> None:
        print(
            "[warn] 'cache-collections' is deprecated; use 'collections cache' instead.",
            file=sys.stderr,
        )
        self.refresh_collections_cache()
