import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from vwcli.cli import run
from vwcli.client import Client
from vwcli.exceptions import VwcliError


pytestmark = pytest.mark.integration


def _run(client: Client, *args: str) -> int:
    try:
        return run(client, ["vwcli", *args])
    except (VwcliError, ValueError):
        return 1
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


def _parse_created_id(output: str) -> str:
    match = re.search(r"Created item: .* \[([a-f0-9\-]+)\]", output)
    if not match:
        raise AssertionError(f"Could not parse created item id from: {output!r}")
    return match.group(1)


def _search_json(client: Client, capsys: pytest.CaptureFixture[str], query: str) -> list[dict]:
    capsys.readouterr()  # discard any previous stdout/stderr in this test
    # Tests drive reads immediately after writes; give bw serve a moment to
    # settle its in-memory search index before asserting on the result.
    time.sleep(0.3)
    rc = _run(client, "search", "--json", query)
    captured = capsys.readouterr()
    assert rc == 0, f"search failed for {query}: {captured.err}"
    return json.loads(captured.out)


def _create_and_search(client: Client, capsys: pytest.CaptureFixture[str], suffix: str) -> None:
    unique = f"vwcli-{suffix}-{uuid.uuid4().hex[:8]}"

    rc = _run(client, "create", "--name", unique, "--username", "user@example.com", "--password", "old-secret", "--uri", "https://example.com")
    assert rc == 0, f"create failed: {unique}"

    items = _search_json(client, capsys, unique)
    assert len(items) == 1
    assert items[0]["name"] == unique
    assert items[0]["login"]["username"] == "user@example.com"
    assert items[0]["login"]["password"] == "old-secret"

    rc = _run(client, "update", "--search", unique, "--password", "new-secret")
    assert rc == 0, f"update failed: {unique}"

    items = _search_json(client, capsys, unique)
    assert len(items) == 1
    assert items[0]["login"]["password"] == "new-secret"

    rc = _run(client, "delete", "--search", unique, "--yes")
    assert rc == 0, f"delete failed: {unique}"

    items = _search_json(client, capsys, unique)
    assert items == []


def test_vwcli_tcp_create_search_update_delete(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    _create_and_search(Client(), capsys, "tcp")


def test_vwcli_unix_socket_create_search_update_delete(integration_env_unix, capsys: pytest.CaptureFixture[str]) -> None:
    _create_and_search(Client(), capsys, "unix")


def test_vwcli_socket_activated_create_search_update_delete(
    integration_env_activated, capsys: pytest.CaptureFixture[str]
) -> None:
    _create_and_search(Client(), capsys, "activated")


@pytest.mark.slow
def test_vwcli_fallback_create_and_search(integration_env_fallback, capsys: pytest.CaptureFixture[str]) -> None:
    """Force the CLI to start and stop its own bw serve for each command."""
    _create_and_search(Client(), capsys, "fallback")


def test_vwcli_create_with_generated_password(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-gen-{uuid.uuid4().hex[:8]}"

    rc = _run(Client(), "create", "--name", unique, "--username", "user@example.com", "--generate-password")
    captured = capsys.readouterr()
    assert rc == 0, f"create --generate-password failed: {captured.err}"
    assert "Generated password:" in captured.out

    items = _search_json(Client(), capsys, unique)
    assert len(items) == 1
    pw = items[0]["login"]["password"]
    assert len(pw) >= 12

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_update_uris_and_clone(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-uris-{uuid.uuid4().hex[:8]}"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "user@example.com",
        "--password",
        "secret",
        "--uri",
        "https://example.com",
        "--uri",
        "https://app.example.com::host",
    )
    assert rc == 0, f"create with multiple uris failed: {unique}"

    items = _search_json(Client(), capsys, unique)
    assert len(items) == 1
    uris = [u["uri"] for u in items[0]["login"]["uris"]]
    assert "https://example.com" in uris
    assert "https://app.example.com" in uris

    rc = _run(Client(), "update", "--search", unique, "--add-uri", "https://new.example.com")
    assert rc == 0, f"update --add-uri failed: {unique}"

    items = _search_json(Client(), capsys, unique)
    uris = [u["uri"] for u in items[0]["login"]["uris"]]
    assert "https://new.example.com" in uris

    rc = _run(Client(), "update", "--search", unique, "--remove-uri", "https://example.com")
    assert rc == 0, f"update --remove-uri failed: {unique}"

    items = _search_json(Client(), capsys, unique)
    uris = [u["uri"] for u in items[0]["login"]["uris"]]
    assert "https://example.com" not in uris
    assert "https://new.example.com" in uris

    rc = _run(Client(), "clone", "--search", unique, "--name", f"{unique}-clone")
    captured = capsys.readouterr()
    assert rc == 0, f"clone failed: {unique}"
    clone_id_match = re.search(r"Cloned item \[[a-f0-9\-]+\] -> \[([a-f0-9\-]+)\]", captured.out)
    assert clone_id_match, f"Could not find clone id in: {captured.out}"
    clone_id = clone_id_match.group(1)

    items = _search_json(Client(), capsys, f"{unique}-clone")
    assert len(items) == 1
    assert items[0]["id"] == clone_id

    _run(Client(), "delete", "--search", unique, "--yes")
    _run(Client(), "delete", "--id", clone_id, "--yes")

    assert _search_json(Client(), capsys, f"{unique}-clone") == []


def test_vwcli_search_filters(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-search-{uuid.uuid4().hex[:8]}"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "user@example.com",
        "--password",
        "secret",
        "--uri",
        "https://filtered.example.com",
    )
    assert rc == 0

    # Exact search works
    assert len(_search_json(Client(), capsys, unique)) == 1

    # URI filter works
    rc = _run(Client(), "search", "--json", "--uri", "https://filtered.example.com")
    captured = capsys.readouterr()
    assert rc == 0, f"uri filter search failed: {captured.err}"
    items = json.loads(captured.out)
    names = [i["name"] for i in items]
    assert unique in names

    # Unknown query returns empty list
    items = _search_json(Client(), capsys, f"{unique}-does-not-exist")
    assert items == []

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_delete_by_id(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-byid-{uuid.uuid4().hex[:8]}"

    rc = _run(Client(), "create", "--name", unique, "--username", "user@example.com", "--password", "secret")
    captured = capsys.readouterr()
    assert rc == 0
    item_id = _parse_created_id(captured.out)

    rc = _run(Client(), "delete", "--id", item_id, "--yes")
    assert rc == 0, f"delete by id failed: {item_id}"

    assert _search_json(Client(), capsys, unique) == []


def test_vwcli_update_name_notes_and_search(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-notes-{uuid.uuid4().hex[:8]}"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "user@example.com",
        "--password",
        "secret",
        "--notes",
        "initial note",
    )
    assert rc == 0

    rc = _run(
        Client(),
        "update",
        "--search",
        unique,
        "--name",
        f"{unique}-renamed",
        "--notes",
        "updated note",
        "--username",
        "renamed@example.com",
    )
    assert rc == 0

    items = _search_json(Client(), capsys, f"{unique}-renamed")
    assert len(items) == 1
    assert items[0]["name"] == f"{unique}-renamed"
    assert items[0]["login"]["username"] == "renamed@example.com"
    assert items[0]["notes"] == "updated note"

    _run(Client(), "delete", "--search", f"{unique}-renamed", "--yes")


def test_vwcli_move_between_collections(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    """Create an item in the Source collection, move it to Target, and verify."""
    unique = f"vwcli-move-{uuid.uuid4().hex[:8]}"

    # Ensure the collection cache is populated
    rc = _run(Client(), "collections", "cache")
    assert rc == 0, "collections cache failed"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "user@example.com",
        "--password",
        "secret",
        "--vault",
        "Test Org",
        "--collection",
        "Source",
    )
    captured = capsys.readouterr()
    assert rc == 0, f"create in org/collection failed: {captured.out} {captured.err}"

    items = _search_json(Client(), capsys, unique)
    assert len(items) == 1
    assert vaultwarden_server.source_collection_id in (items[0].get("collectionIds") or [])

    rc = _run(Client(), "move", "--from", "Source", "--to", "Target", "--search", unique, "--yes")
    captured = capsys.readouterr()
    assert rc == 0, f"move failed: {captured.out} {captured.err}"
    assert "Moved" in captured.out

    items = _search_json(Client(), capsys, unique)
    assert len(items) == 1
    assert vaultwarden_server.target_collection_id in (items[0].get("collectionIds") or [])
    assert vaultwarden_server.source_collection_id not in (items[0].get("collectionIds") or [])

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_negative_cases(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-neg-{uuid.uuid4().hex[:8]}"

    # Search for something that does not exist returns an empty JSON list
    items = _search_json(Client(), capsys, f"{unique}-missing")
    assert items == []

    # Update something that does not exist
    rc = _run(Client(), "update", "--search", f"{unique}-missing", "--password", "x")
    assert rc == 1, "expected non-zero exit for update of missing item"

    # Delete something that does not exist
    rc = _run(Client(), "delete", "--search", f"{unique}-missing", "--yes")
    assert rc == 1, "expected non-zero exit for delete of missing item"


def test_vwcli_edge_cases(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    """Exercise parser and command-level error paths."""
    unique = f"vwcli-edge-{uuid.uuid4().hex[:8]}"

    # create requires --name
    rc = _run(Client(), "create", "--username", "u", "--password", "p")
    assert rc != 0, "expected create without --name to fail"

    # update/clone require --id or --search
    rc = _run(Client(), "update", "--name", "foo")
    assert rc != 0, "expected update without selector to fail"
    rc = _run(Client(), "clone", "--name", "foo")
    assert rc != 0, "expected clone without selector to fail"

    # delete requires --id or --search
    rc = _run(Client(), "delete", "--yes")
    assert rc != 0, "expected delete without selector to fail"

    # create two items with the same name; search should fail because not unique
    rc = _run(Client(), "create", "--name", unique, "--username", "a", "--password", "p1")
    assert rc == 0
    rc = _run(Client(), "create", "--name", unique, "--username", "b", "--password", "p2")
    assert rc == 0
    time.sleep(0.3)
    rc = _run(Client(), "update", "--search", unique, "--password", "x")
    captured = capsys.readouterr()
    assert rc == 1, f"expected update of non-unique {unique} to fail: {captured.out}"

    # delete both via id
    for _ in range(2):
        items = _search_json(Client(), capsys, unique)
        if items:
            _run(Client(), "delete", "--id", items[0]["id"], "--yes")
            time.sleep(0.3)

    # search with no query and no uri filter
    rc = _run(Client(), "search")
    assert rc == 1, "expected bare search without query to fail"

    # move without both collections
    rc = _run(Client(), "move", "--from", "Source")
    assert rc != 0, "expected move without --to to fail"

    # attachment without selector
    rc = _run(Client(), "attachment")
    assert rc == 0, "expected bare attachment to list and print usage"

    # collections without subcommand
    rc = _run(Client(), "collections")
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage:" in captured.err or "Usage:" in captured.out


def _extract_ansible_vault_blob(output: str) -> str:
    """Parse the YAML-ish output of `ansible-vault encrypt_string` and return the raw vault blob."""
    lines: list[str] = []
    collecting = False
    for line in output.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("$ANSIBLE_VAULT;"):
            collecting = True
            lines.append(stripped)
        elif collecting:
            if not stripped or not all(c in "0123456789abcdefABCDEF" for c in stripped):
                break
            lines.append(stripped)
    if not lines:
        raise AssertionError(f"Could not find an ansible-vault blob in: {output!r}")
    return "\n".join(lines)


def _decrypt_ansible_vault_blob(blob: str, password_file: Path) -> str:
    """Decrypt an ansible-vault blob and return the plaintext."""
    with tempfile.NamedTemporaryFile("w", suffix=".vault", delete=False) as tmp:
        tmp.write(blob)
        tmp_path = tmp.name
    try:
        env = os.environ.copy()
        env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(password_file)
        run = subprocess.run(
            ["ansible-vault", "decrypt", tmp_path, "--output", "-"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if run.returncode != 0:
            raise AssertionError(f"ansible-vault decrypt failed: {run.stderr}")
        return run.stdout
    finally:
        os.unlink(tmp_path)


def test_vwcli_ansible_vault_create(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-ansible-create-{uuid.uuid4().hex[:8]}"
    password = "super-secret-ansible-password"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "ansible@example.com",
        "--password",
        password,
        "--to-ansible-vault",
    )
    captured = capsys.readouterr()
    assert rc == 0, f"create --to-ansible-vault failed: {captured.err}"
    assert "Created item" in captured.out

    blob = _extract_ansible_vault_blob(captured.out)
    decrypted = _decrypt_ansible_vault_blob(blob, vaultwarden_server.ansible_vault_password_file)
    assert decrypted == password

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_ansible_vault_search(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-ansible-search-{uuid.uuid4().hex[:8]}"
    password = "searchable-ansible-password"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "ansible@example.com",
        "--password",
        password,
    )
    assert rc == 0

    rc = _run(Client(), "search", unique, "--to-ansible-vault")
    captured = capsys.readouterr()
    assert rc == 0, f"search --to-ansible-vault failed: {captured.err}"

    blob = _extract_ansible_vault_blob(captured.out)
    decrypted = _decrypt_ansible_vault_blob(blob, vaultwarden_server.ansible_vault_password_file)
    assert decrypted == password

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_ansible_vault_update(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-ansible-update-{uuid.uuid4().hex[:8]}"
    new_password = "updated-ansible-password"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "ansible@example.com",
        "--password",
        "initial-password",
    )
    assert rc == 0

    # Let the bw serve index settle before the search-driven update.
    time.sleep(0.3)

    rc = _run(Client(), "update", "--search", unique, "--password", new_password, "--to-ansible-vault")
    captured = capsys.readouterr()
    assert rc == 0, f"update --to-ansible-vault failed: {captured.err}"
    assert "Updated item" in captured.out

    blob = _extract_ansible_vault_blob(captured.out)
    decrypted = _decrypt_ansible_vault_blob(blob, vaultwarden_server.ansible_vault_password_file)
    assert decrypted == new_password

    _run(Client(), "delete", "--search", unique, "--yes")


def test_vwcli_ansible_vault_clone(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    unique = f"vwcli-ansible-clone-{uuid.uuid4().hex[:8]}"
    password = "clonable-ansible-password"

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "ansible@example.com",
        "--password",
        password,
    )
    assert rc == 0

    rc = _run(Client(), "clone", "--search", unique, "--name", f"{unique}-clone", "--to-ansible-vault")
    captured = capsys.readouterr()
    assert rc == 0, f"clone --to-ansible-vault failed: {captured.err}"
    assert "Cloned item" in captured.out

    blob = _extract_ansible_vault_blob(captured.out)
    decrypted = _decrypt_ansible_vault_blob(blob, vaultwarden_server.ansible_vault_password_file)
    assert decrypted == password

    _run(Client(), "delete", "--search", unique, "--yes")
    _run(Client(), "delete", "--search", f"{unique}-clone", "--yes")


def test_vwcli_login(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    """Call the login command non-interactively using BW_PASSWORD and verify a session is cached."""
    rc = _run(Client(), "login")
    captured = capsys.readouterr()
    assert rc == 0, f"login failed: {captured.out} {captured.err}"
    assert "BW_SESSION cached" in captured.out


def test_vwcli_attachment_add_list_delete(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    """Create an item, attach a file, list it, delete it, and verify it is gone."""
    unique = f"vwcli-attach-{uuid.uuid4().hex[:8]}"
    attach_file = integration_env / f"{unique}.txt"
    attach_file.write_text("attachment payload", encoding="utf-8")

    rc = _run(
        Client(),
        "create",
        "--name",
        unique,
        "--username",
        "user@example.com",
        "--password",
        "secret",
    )
    captured = capsys.readouterr()
    assert rc == 0
    item_id = _parse_created_id(captured.out)
    assert item_id

    time.sleep(0.3)
    rc = _run(Client(), "attachment", "add", "--id", item_id, "--file", str(attach_file))
    captured = capsys.readouterr()
    assert rc == 0, f"attachment add failed: {captured.out} {captured.err}"
    assert "Attachment added" in captured.out

    match = re.search(r"Attachment added to item .*\[([^\]]+)\]", captured.out)
    assert match, f"Could not parse attachment id from: {captured.out!r}"
    attachment_id = match.group(1)

    time.sleep(0.3)
    rc = _run(Client(), "attachment", "list", "--id", item_id, "--json")
    captured = capsys.readouterr()
    assert rc == 0, f"attachment list failed: {captured.err}"
    attachments = json.loads(captured.out)
    assert any(a.get("id") == attachment_id for a in attachments), attachments

    rc = _run(Client(), "attachment", "delete", "--id", item_id, "--attachment-id", attachment_id)
    captured = capsys.readouterr()
    assert rc == 0, f"attachment delete failed: {captured.err}"
    assert "deleted" in captured.out

    time.sleep(0.3)
    rc = _run(Client(), "attachment", "list", "--id", item_id, "--json")
    captured = capsys.readouterr()
    assert rc == 0
    attachments = json.loads(captured.out)
    assert not any(a.get("id") == attachment_id for a in attachments), attachments

    _run(Client(), "delete", "--search", unique, "--yes")


def _find_collection_id(name: str, output: str) -> str | None:
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


def test_vwcli_collections_lifecycle(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    """Cache, list, add, search, update and delete a collection."""
    unique = f"vwcli-col-{uuid.uuid4().hex[:8]}"
    renamed = f"{unique}-renamed"

    rc = _run(Client(), "collections", "cache")
    assert rc == 0, "collections cache failed"

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    assert rc == 0

    rc = _run(Client(), "collections", "add", "--name", unique, "--organization-id", vaultwarden_server.org_id)
    captured = capsys.readouterr()
    assert rc == 0, f"collections add failed: {captured.out} {captured.err}"

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    assert rc == 0
    col_id = _find_collection_id(unique, captured.out)
    assert col_id, f"added collection {unique} not found in list output: {captured.out!r}"

    rc = _run(Client(), "collections", "search", unique)
    captured = capsys.readouterr()
    assert rc == 0
    assert unique in captured.out

    time.sleep(0.3)
    rc = _run(Client(), "collections", "update", "--id", col_id, "--name", renamed)
    captured = capsys.readouterr()
    assert rc == 0, f"collections update failed: {captured.err}"
    assert "Updated collection" in captured.out

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    assert rc == 0
    assert _find_collection_id(renamed, captured.out) == col_id

    rc = _run(Client(), "collections", "delete", "--id", col_id, "--yes")
    captured = capsys.readouterr()
    assert rc == 0, f"collections delete failed: {captured.err}"
    assert "Deleted collection" in captured.out

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    assert rc == 0
    assert _find_collection_id(renamed, captured.out) is None


def test_vwcli_collections_move(integration_env, vaultwarden_server, capsys: pytest.CaptureFixture[str]) -> None:
    """Create a parent and child collection, then move the child under the parent."""
    parent_name = f"vwcli-parent-{uuid.uuid4().hex[:8]}"
    child_name = f"vwcli-child-{uuid.uuid4().hex[:8]}"

    rc = _run(Client(), "collections", "add", "--name", parent_name, "--organization-id", vaultwarden_server.org_id)
    captured = capsys.readouterr()
    assert rc == 0, f"collections add parent failed: {captured.out} {captured.err}"

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    parent_id = _find_collection_id(parent_name, captured.out)
    assert parent_id

    rc = _run(Client(), "collections", "add", "--name", child_name, "--organization-id", vaultwarden_server.org_id)
    captured = capsys.readouterr()
    assert rc == 0, f"collections add child failed: {captured.out} {captured.err}"

    time.sleep(0.3)
    rc = _run(Client(), "collections", "list")
    captured = capsys.readouterr()
    child_id = _find_collection_id(child_name, captured.out)
    assert child_id

    rc = _run(Client(), "collections", "move", "--id", child_id, "--to-parent-id", parent_id)
    captured = capsys.readouterr()
    assert rc == 0, f"collections move failed: {captured.err}"
    assert "Moved collection" in captured.out

    time.sleep(0.3)
    client = Client()
    current = client.bw_get_collection(child_id)
    data = current.get("data", {}) if isinstance(current, dict) else {}
    assert data.get("id") == child_id

    rc = _run(Client(), "collections", "delete", "--id", child_id, "--yes")
    assert rc == 0
    rc = _run(Client(), "collections", "delete", "--id", parent_id, "--yes")
    assert rc == 0
