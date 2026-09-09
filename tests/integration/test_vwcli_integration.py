import uuid

import pytest

from vwcli.cli import run
from vwcli.client import Client


pytestmark = pytest.mark.integration


def _run(client: Client, *args: str) -> int:
    return run(client, ["vwcli", *args])


def test_vwcli_create_search_update_delete(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    client = Client()
    unique = f"vwcli-integration-{uuid.uuid4().hex[:8]}"

    rc = _run(client, "create", "--name", unique, "--username", "user@example.com", "--password", "old-secret", "--uri", "https://example.com")
    assert rc == 0, f"create failed: {unique}"

    rc = _run(client, "search", "--json", unique)
    captured = capsys.readouterr()
    assert rc == 0, f"search failed: {unique}"
    assert unique in captured.out
    assert '"password": "old-secret"' in captured.out

    rc = _run(client, "update", "--search", unique, "--password", "new-secret")
    assert rc == 0, f"update failed: {unique}"

    rc = _run(client, "search", "--json", unique)
    captured = capsys.readouterr()
    assert rc == 0, f"search after update failed: {unique}"
    assert unique in captured.out
    assert '"password": "new-secret"' in captured.out

    rc = _run(client, "delete", "--search", unique, "--yes")
    assert rc == 0, f"delete failed: {unique}"

    rc = _run(client, "search", "--json", unique)
    captured = capsys.readouterr()
    assert rc == 0, f"search after delete failed: {unique}"
    assert unique not in captured.out
    assert "[]" in captured.out
