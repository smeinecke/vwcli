from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from vaultwarden.models.bitwarden import Kdf
from vaultwarden.models.enum import KdfType
from vaultwarden.utils.crypto import (
    encrypt_asym,
    encrypt_sym,
    make_asym_key,
    make_master_key,
    make_sym_key,
    token_bytes,
)


@dataclasses.dataclass
class VaultwardenServer:
    """Holds connection details for the ephemeral test Vaultwarden container."""

    url: str
    email: str
    password: str
    session: str
    home: Path
    tls_dir: Path
    serve_procs: list[subprocess.Popen[str]] = dataclasses.field(default_factory=list)
    org_id: str = ""
    source_collection_id: str = ""
    target_collection_id: str = ""
    ansible_vault_password_file: Path = dataclasses.field(default_factory=Path)


TEST_EMAIL = "integration-test@example.com"
TEST_PASSWORD = "TestP@ssw0rd!"
TEST_HOST = "127.0.0.1"
TEST_PORT = 18443


def _compose_file() -> Path:
    return Path(__file__).with_name("docker-compose.yml").resolve()


def _socket_patch_path() -> Path:
    return Path(__file__).parents[2] / "bitwarden-serve" / "bw-unix-socket-patch.js"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((TEST_HOST, 0))
        return int(sock.getsockname()[1])


def _generate_certs(tls_dir: Path) -> None:
    tls_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={TEST_HOST}",
            "-addext",
            f"subjectAltName=IP:{TEST_HOST},DNS:localhost",
            "-keyout",
            str(tls_dir / "key.pem"),
            "-out",
            str(tls_dir / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )


def _bw_access_token_and_uuid(home: Path) -> tuple[str, str]:
    """Read the Bitwarden CLI access token and user UUID from its data file."""
    data_file = home / ".config" / "Bitwarden CLI" / "data.json"
    data = json.loads(data_file.read_text(encoding="utf-8"))
    token_key = next(k for k in data if k.endswith("_token_accessToken"))
    match = re.match(r"user_(.+?)_token_accessToken", token_key)
    if not match:
        raise RuntimeError(f"Could not parse user UUID from token key {token_key!r}")
    user_uuid = match.group(1)
    return str(data[token_key]), user_uuid


def _create_organization_and_collections(server: VaultwardenServer) -> None:
    """Provision a test organization with Source and Target collections."""
    env = os.environ.copy()
    env["HOME"] = str(server.home)
    env["XDG_CONFIG_HOME"] = str(server.home / ".config")
    env["BW_SESSION"] = server.session
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    access_token, user_uuid = _bw_access_token_and_uuid(server.home)
    headers = {"Authorization": f"Bearer {access_token}"}

    # Fetch the user's RSA public key so we can encrypt the org symmetric key.
    resp = requests.get(f"{server.url}/api/users/{user_uuid}/public-key", headers=headers, verify=False)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch user public key: {resp.status_code} {resp.text}")
    user_public_key_b64 = resp.json()["publicKey"]
    user_public_key_der = base64.b64decode(user_public_key_b64)

    # Org symmetric key (64 bytes) and RSA key pair.
    org_key = token_bytes(64)
    encrypted_org_private_key, org_public_key, _ = make_asym_key(org_key)
    encrypted_org_key = encrypt_asym(org_key, user_public_key_der)

    org_payload = {
        "name": "Test Org",
        "billingEmail": server.email,
        "collectionName": encrypt_sym("Source", org_key),
        "key": encrypted_org_key,
        "keys": {
            "encryptedPrivateKey": encrypted_org_private_key,
            "publicKey": base64.b64encode(org_public_key).decode(),
        },
        "planType": "0",
    }
    resp = requests.post(f"{server.url}/api/organizations", json=org_payload, headers=headers, verify=False)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not create organization: {resp.status_code} {resp.text[:500]}")
    org_id = resp.json()["id"]
    server.org_id = str(org_id)

    # Create the second collection using the same org key.
    target_name = encrypt_sym("Target", org_key)
    target_resp = requests.post(
        f"{server.url}/api/organizations/{org_id}/collections",
        json={"name": target_name, "groups": [], "users": []},
        headers=headers,
        verify=False,
    )
    if target_resp.status_code != 200:
        raise RuntimeError(f"Could not create Target collection: {target_resp.status_code} {target_resp.text[:500]}")
    server.target_collection_id = str(target_resp.json()["id"])

    # Sync the CLI state so bw sees the new organization and collections.
    sync = subprocess.run(["bw", "sync"], env=env, capture_output=True, text=True)
    if sync.returncode != 0:
        raise RuntimeError(f"bw sync failed: {sync.stderr}")

    run = subprocess.run(["bw", "list", "org-collections", "--organizationid", server.org_id], env=env, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"bw list org-collections failed: {run.stderr}")
    for col in json.loads(run.stdout):
        if col.get("name") == "Source":
            server.source_collection_id = str(col.get("id"))
    if not server.source_collection_id or not server.target_collection_id:
        raise RuntimeError("Could not resolve Source or Target collection ids")


def _wait_for_vaultwarden(url: str, timeout: float = 60.0) -> None:
    session = requests.Session()
    session.verify = False
    retries = Retry(total=10, backoff_factor=0.2, status_forcelist=[503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = session.get(f"{url}/alive", timeout=5)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Vaultwarden did not become ready at {url}")


def _register_user(url: str, email: str, password: str) -> None:
    kdf = Kdf(Kdf=KdfType.Pbkdf2, KdfIterations=600_000, KdfMemory=None, KdfParallelism=None)
    master_key = make_master_key(password, email, kdf)
    master_password_hash = base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", master_key, password.encode("utf-8"), 1)
    ).decode()
    encrypted_symmetric_key, symmetric_key = make_sym_key(master_key)
    encrypted_private_key, public_key, _ = make_asym_key(symmetric_key, stretch=False)

    payload = {
        "email": email,
        "masterPasswordHash": master_password_hash,
        "masterPasswordHint": "",
        "key": encrypted_symmetric_key,
        "kdfType": 0,
        "iterations": 600_000,
        "name": "Integration Test",
        "keys": {
            "publicKey": base64.b64encode(public_key).decode(),
            "encryptedPrivateKey": encrypted_private_key,
        },
    }
    resp = requests.post(f"{url}/identity/accounts/register", json=payload, verify=False)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to register test user: {resp.status_code} {resp.text[:500]}")


def _bw_login(url: str, email: str, password: str, home: Path) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    subprocess.run(["bw", "logout"], env=env, capture_output=True, check=False)
    subprocess.run(["bw", "config", "server", url], env=env, capture_output=True, check=True)
    run = subprocess.run(
        ["bw", "login", email, password, "--raw"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"bw login failed: {run.stderr}")
    return run.stdout.strip()


def _assert_no_tcp_listener(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    ss = shutil.which("ss")
    if not ss:
        return
    try:
        run = subprocess.run([ss, "-ltnp"], capture_output=True, text=True, check=False, timeout=2)
    except Exception:
        return
    if f"pid={proc.pid}" in run.stdout:
        raise RuntimeError(f"bw serve (pid {proc.pid}) is listening on TCP; expected Unix socket only")


def _ping_serve_url(serve_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if serve_url.startswith("unix://"):
                sock_path = serve_url[len("unix://") :]
                conn = http.client.HTTPConnection("localhost", timeout=1)
                conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.sock.settimeout(1)
                conn.sock.connect(sock_path)
                conn.request("GET", "/status")
                resp = conn.getresponse()
                if resp.status == 200:
                    conn.close()
                    return
                conn.close()
            else:
                resp = requests.get(f"{serve_url}/status", timeout=1)
                if resp.status_code == 200:
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"bw serve did not become ready at {serve_url}")


def _start_bw_serve_tcp(home: Path, session: str) -> tuple[str, subprocess.Popen[str]]:
    port = _free_port()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["BW_SESSION"] = session
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    proc = subprocess.Popen(
        ["bw", "serve", "--hostname", TEST_HOST, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    serve_url = f"http://{TEST_HOST}:{port}"
    if proc.poll() is not None:
        raise RuntimeError("bw serve (tcp) exited immediately")
    _ping_serve_url(serve_url, timeout=20.0)
    return serve_url, proc


def _start_bw_serve_unix(home: Path, session: str) -> tuple[str, subprocess.Popen[str]]:
    socket_path = home / "bw.sock"
    socket_path_str = str(socket_path)
    patch_path = _socket_patch_path()
    node = shutil.which("node")
    bw = shutil.which("bw")
    if not node or not bw:
        raise RuntimeError("node and bw are required for the unix socket service test")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["BW_SESSION"] = session
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    env["BW_SERVE_SOCKET"] = socket_path_str
    proc = subprocess.Popen(
        [node, "--require", str(patch_path), bw, "serve", "--hostname", TEST_HOST, "--port", "8087"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    serve_url = f"unix://{socket_path_str}"
    if proc.poll() is not None:
        raise RuntimeError("bw serve (unix) exited immediately")
    _ping_serve_url(serve_url, timeout=20.0)
    _assert_no_tcp_listener(proc)
    return serve_url, proc


@pytest.fixture(scope="session")
def vaultwarden_server(tmp_path_factory: pytest.TempPathFactory) -> Generator[VaultwardenServer, None, None]:
    """Start a Vaultwarden container, register a demo user and log in."""
    home = tmp_path_factory.mktemp("vwcli-integration-home")
    tls_dir = tmp_path_factory.mktemp("vwcli-integration-tls")
    _generate_certs(tls_dir)

    env = os.environ.copy()
    env["TLS_DIR"] = str(tls_dir)

    compose_file = _compose_file()
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", "vwcli-integration-test", "down", "-v"],
        env=env,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", "vwcli-integration-test", "up", "-d"],
        env=env,
        check=True,
    )

    url = f"https://{TEST_HOST}:{TEST_PORT}"
    _wait_for_vaultwarden(url)
    _register_user(url, TEST_EMAIL, TEST_PASSWORD)
    session = _bw_login(url, TEST_EMAIL, TEST_PASSWORD, home)

    server = VaultwardenServer(url=url, email=TEST_EMAIL, password=TEST_PASSWORD, session=session, home=home, tls_dir=tls_dir)
    _create_organization_and_collections(server)

    # Provide an ansible-vault password file so ansible-vault E2E tests can run
    ansible_vault_file = home / "ansible-vault-password.txt"
    ansible_vault_file.write_text("integration-vault-password", encoding="utf-8")
    server.ansible_vault_password_file = ansible_vault_file

    yield server

    for proc in server.serve_procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", "vwcli-integration-test", "down", "-v"],
        env=env,
        capture_output=True,
        check=False,
    )
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(tls_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def bw_serve_url_tcp(vaultwarden_server: VaultwardenServer) -> str:
    """Start a TCP bw serve and return its URL."""
    serve_url, proc = _start_bw_serve_tcp(vaultwarden_server.home, vaultwarden_server.session)
    vaultwarden_server.serve_procs.append(proc)
    return serve_url


@pytest.fixture(scope="session")
def bw_serve_url_unix(vaultwarden_server: VaultwardenServer) -> str:
    """Start a Unix socket bw serve and return its URL."""
    serve_url, proc = _start_bw_serve_unix(vaultwarden_server.home, vaultwarden_server.session)
    vaultwarden_server.serve_procs.append(proc)
    return serve_url


@pytest.fixture()
def integration_env(
    vaultwarden_server: VaultwardenServer,
    bw_serve_url_tcp: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide env for an integration test using the TCP bw serve."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(vaultwarden_server.home / ".config"))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_tcp)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    if vaultwarden_server.ansible_vault_password_file:
        monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(vaultwarden_server.ansible_vault_password_file))
    return vaultwarden_server.home


@pytest.fixture(params=["tcp", "unix"])
def integration_env_both(
    request: pytest.FixtureRequest,
    vaultwarden_server: VaultwardenServer,
    bw_serve_url_tcp: str,
    bw_serve_url_unix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run the test once against the TCP serve and once against the Unix socket serve."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(vaultwarden_server.home / ".config"))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    if request.param == "unix":
        monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_unix)
    else:
        monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_tcp)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    if vaultwarden_server.ansible_vault_password_file:
        monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(vaultwarden_server.ansible_vault_password_file))
    return vaultwarden_server.home


@pytest.fixture()
def integration_env_unix(
    vaultwarden_server: VaultwardenServer,
    bw_serve_url_unix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide env for an integration test using the Unix socket bw serve."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(vaultwarden_server.home / ".config"))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_unix)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    if vaultwarden_server.ansible_vault_password_file:
        monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(vaultwarden_server.ansible_vault_password_file))
    return vaultwarden_server.home


@pytest.fixture()
def integration_env_fallback(
    vaultwarden_server: VaultwardenServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide env for an integration test that falls back to bw CLI commands."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(vaultwarden_server.home / ".config"))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.delenv("BW_SERVE_URL", raising=False)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    if vaultwarden_server.ansible_vault_password_file:
        monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(vaultwarden_server.ansible_vault_password_file))
    return vaultwarden_server.home
