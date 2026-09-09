import uuid

import pytest

from vwcli.cli import run
from vwcli.client import Client


pytestmark = pytest.mark.integration


def _run(client: Client, *args: str) -> int:
    return run(client, ["vwcli", *args])


def _create_and_search(client: Client, capsys: pytest.CaptureFixture[str], suffix: str) -> None:
    unique = f"vwcli-{suffix}-{uuid.uuid4().hex[:8]}"

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


def test_vwcli_tcp_create_search_update_delete(integration_env, capsys: pytest.CaptureFixture[str]) -> None:
    _create_and_search(Client(), capsys, "tcp")


def test_vwcli_unix_socket_create_search_update_delete(integration_env_unix, capsys: pytest.CaptureFixture[str]) -> None:
    _create_and_search(Client(), capsys, "unix")


@pytest.mark.slow
def test_vwcli_fallback_create_and_search(integration_env_fallback, capsys: pytest.CaptureFixture[str]) -> None:
    """Force the CLI to start and stop its own bw serve for each command."""
    _create_and_search(Client(), capsys, "fallback")
