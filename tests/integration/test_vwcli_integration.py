import json
import re
import uuid

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


def _parse_created_id(output: str) -> str:
    match = re.search(r"Created item: .* \[([a-f0-9\-]+)\]", output)
    if not match:
        raise AssertionError(f"Could not parse created item id from: {output!r}")
    return match.group(1)


def _search_json(client: Client, capsys: pytest.CaptureFixture[str], query: str) -> list[dict]:
    capsys.readouterr()  # discard any previous stdout/stderr in this test
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
