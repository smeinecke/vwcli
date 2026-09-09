import argparse
import json
from pathlib import Path

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


def test_config_migrates_old_paths(tmp_path: Path, monkeypatch) -> None:
    _patch_constants(monkeypatch, tmp_path)
    old_config_dir = tmp_path / ".config" / "pws"
    old_config_file = old_config_dir / "config"
    old_cache_dir = tmp_path / ".cache" / "bw-cli"
    old_config_dir.mkdir(parents=True)
    old_cache_dir.mkdir(parents=True)
    old_config_file.write_text("BW_SESSION=old-token\n", encoding="utf-8")
    (old_cache_dir / "collections.json").write_text(
        json.dumps({"data": []}), encoding="utf-8"
    )

    cfg = config.Config()
    assert cfg.config_file.exists()
    assert cfg.config_file.read_text() == "BW_SESSION=old-token\n"
    assert cfg.collection_cache.exists()


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
