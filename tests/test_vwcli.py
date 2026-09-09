import argparse
from pathlib import Path
from typing import cast

import pytest

import vwcli.constants as constants
import vwcli.config as config


def _patch_constants(monkeypatch, tmp_path: Path) -> Path:
    """Patch vwcli.constants so Config uses tmp_path for the duration of a test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "vwcli"
    cache_dir = tmp_path / ".cache" / "vwcli"
    monkeypatch.setattr(constants, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(constants, "CONFIG_FILE", cfg_dir / "config")
    monkeypatch.setattr(constants, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(constants, "COLLECTION_CACHE", cache_dir / "collections.json")
    return tmp_path


def test_parse_uri_default() -> None:
    assert constants.parse_uri("https://example.com") == ("https://example.com", None)


def test_parse_uri_with_match() -> None:
    assert constants.parse_uri("https://example.com::host") == (
        "https://example.com",
        1,
    )


def test_parse_uri_invalid() -> None:
    with pytest.raises(ValueError, match="Unknown URI match type"):
        constants.parse_uri("https://example.com::nope")


def test_config_creates_secure_dirs(tmp_path: Path, monkeypatch) -> None:
    _patch_constants(monkeypatch, tmp_path)
    cfg = config.Config()
    cfg.ensure_dir()
    assert cfg.config_dir.exists()
    assert cfg.config_dir.stat().st_mode & 0o777 == 0o700
    cfg.ensure_cache_dir()
    assert cfg.cache_dir.exists()
    assert cfg.cache_dir.stat().st_mode & 0o777 == 0o700


def test_config_set_and_load(tmp_path: Path, monkeypatch) -> None:
    _patch_constants(monkeypatch, tmp_path)
    cfg = config.Config()

    class DummyClient:
        bw_session = ""
        bw_session_expires = 0
        bw_serve_url = ""

    dummy = DummyClient()
    cfg.set("BW_SESSION", "test-token")
    cfg.load(dummy)
    assert dummy.bw_session == "test-token"
    assert cfg.config_file.read_text() == "BW_SESSION=test-token\n"


def test_build_parser_prog() -> None:
    from vwcli.cli import build_parser

    parser = build_parser()
    assert parser.prog == "vwcli"


def test_build_parser_known_commands() -> None:
    from vwcli.cli import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert subparsers
    choices = set(subparsers[0].choices)
    assert {"login", "search", "create", "collections", "attachment"} <= choices


def _parse(args: list[str]) -> argparse.Namespace:
    from vwcli.cli import build_parser

    return build_parser().parse_args(args)


class _FakeClient:
    """Minimal stand-in for Client to exercise cli.run argument routing."""

    def __init__(self) -> None:
        self.last_ns: argparse.Namespace | None = None

    def need_bw(self) -> None:
        pass

    def with_bw_serve(self, func, ns: argparse.Namespace) -> None:
        func(ns)

    def cmd_login(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_create(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_update(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_search(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_delete(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_move(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_attachment(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_collections(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns

    def cmd_cache_collections(self, ns: argparse.Namespace) -> None:
        self.last_ns = ns


def _run_fake(argv: list[str]) -> argparse.Namespace:
    from vwcli.cli import Client, run

    client = _FakeClient()
    run(cast(Client, client), argv)
    assert client.last_ns is not None
    return client.last_ns


def test_cli_routes_bare_query_to_search() -> None:
    ns = _run_fake(["vwcli", "myquery"])
    assert ns.command == "search"
    assert ns.query == ["myquery"]


def test_cli_cache_collections_alias() -> None:
    ns = _run_fake(["vwcli", "cache-collections"])
    assert ns.command == "collections"
    assert ns.collections_command == "cache"


def test_build_parser_create() -> None:
    ns = _parse(["create", "--name", "foo", "--username", "user", "--password", "secret", "--uri", "https://example.com::host"])
    assert ns.command == "create"
    assert ns.name == "foo"
    assert ns.username == "user"
    assert ns.password == "secret"
    assert ns.uris == ["https://example.com::host"]
    assert ns.generate_password is False


def test_build_parser_create_generate_password_excludes_password() -> None:
    with pytest.raises(SystemExit):
        _parse(["create", "--name", "foo", "--password", "secret", "--generate-password"])


def test_build_parser_update_requires_selector() -> None:
    with pytest.raises(SystemExit):
        _parse(["update", "--name", "foo"])


def test_build_parser_update_id_and_clone() -> None:
    ns = _parse(["update", "--id", "abc", "--name", "foo", "--clone"])
    assert ns.command == "update"
    assert ns.id == "abc"
    assert ns.clone is True


def test_build_parser_clone_forces_clone() -> None:
    ns = _parse(["clone", "--id", "abc", "--name", "foo"])
    assert ns.command == "clone"
    assert ns.clone is True


def test_build_parser_search_options() -> None:
    ns = _parse(["search", "foo", "bar", "--uri", "example.com", "--json", "--limit", "5"])
    assert ns.command == "search"
    assert ns.query == ["foo", "bar"]
    assert ns.uri_filter == "example.com"
    assert ns.output_json is True
    assert ns.limit == 5


def test_build_parser_delete_id() -> None:
    ns = _parse(["delete", "--id", "abc", "--yes"])
    assert ns.command == "delete"
    assert ns.id == "abc"
    assert ns.yes is True


def test_build_parser_delete_requires_selector() -> None:
    with pytest.raises(SystemExit):
        _parse(["delete"])


def test_build_parser_move() -> None:
    ns = _parse(["move", "--from", "Source", "--to", "Target", "--search", "foo", "--dry-run"])
    assert ns.command == "move"
    assert ns.from_collection == "Source"
    assert ns.to_collection == "Target"
    assert ns.search == "foo"
    assert ns.dry_run is True


def test_build_parser_collections_cache() -> None:
    ns = _parse(["collections", "cache"])
    assert ns.command == "collections"
    assert ns.collections_command == "cache"


def test_build_parser_collections_list() -> None:
    ns = _parse(["collections", "list", "--refresh-cache"])
    assert ns.collections_command == "list"
    assert ns.refresh_cache is True


def test_build_parser_collections_add() -> None:
    ns = _parse(["collections", "add", "--name", "New"])
    assert ns.collections_command == "add"
    assert ns.name == "New"


def test_build_parser_collections_search() -> None:
    ns = _parse(["collections", "search", "foo", "bar"])
    assert ns.collections_command == "search"
    assert ns.query == ["foo", "bar"]


def test_build_parser_collections_update() -> None:
    ns = _parse(["collections", "update", "--id", "cid", "--name", "Renamed", "--parent-id", "pid"])
    assert ns.collections_command == "update"
    assert ns.id == "cid"
    assert ns.name == "Renamed"
    assert ns.parent_id == "pid"


def test_build_parser_collections_delete() -> None:
    ns = _parse(["collections", "delete", "--id", "cid", "--yes"])
    assert ns.collections_command == "delete"
    assert ns.id == "cid"
    assert ns.yes is True


def test_build_parser_collections_move() -> None:
    ns = _parse(["collections", "move", "--id", "cid", "--to-parent-id", "pid"])
    assert ns.collections_command == "move"
    assert ns.id == "cid"
    assert ns.to_parent_id == "pid"


def test_build_parser_attachment_top_level_list() -> None:
    ns = _parse(["attachment", "--search", "foo", "--json"])
    assert ns.command == "attachment"
    assert ns.search == "foo"
    assert ns.output_json is True
    assert ns.attachment_command is None


def test_build_parser_attachment_add() -> None:
    ns = _parse(["attachment", "add", "--id", "iid", "--file", "/tmp/f"])
    assert ns.command == "attachment"
    assert ns.attachment_command == "add"
    assert ns.id == "iid"
    assert ns.file == "/tmp/f"


def test_build_parser_attachment_delete() -> None:
    ns = _parse(["attachment", "delete", "--search", "foo", "--attachment-id", "aid"])
    assert ns.attachment_command == "delete"
    assert ns.attachment_id == "aid"


def test_build_parser_login() -> None:
    ns = _parse(["login"])
    assert ns.command == "login"
