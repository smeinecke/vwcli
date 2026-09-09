"""Shared constants and helpers."""

import os
import re
from pathlib import Path

BW_SESSION_DEFAULT_TTL = int(
    os.environ.get("BW_SESSION_TTL_SECONDS", "2505600")
)  # 29 days (conservative vs 30-day refresh token)

URI_MATCH_NAMES: dict[str, int | None] = {
    "default": None,
    "null": None,
    "base_domain": 0,
    "basedomain": 0,
    "host": 1,
    "starts_with": 2,
    "startswith": 2,
    "exact": 3,
    "regexp": 4,
    "regex": 4,
    "never": 5,
}


def parse_uri(raw: str) -> tuple[str, int | None]:
    """Parse 'URL::match_type' into (url, match_int_or_None). Separator '::' avoids clashing with URL colons."""
    if "::" in raw:
        url, _, match_name = raw.rpartition("::")
        match_name = match_name.lower()
        if match_name not in URI_MATCH_NAMES:
            valid = ", ".join(sorted(URI_MATCH_NAMES))
            raise ValueError(f"Unknown URI match type '{match_name}'. Valid: {valid}")
        return url, URI_MATCH_NAMES[match_name]
    return raw, None


CACHE_DIR = Path.home() / ".cache" / "vwcli"
COLLECTION_CACHE = CACHE_DIR / "collections.json"
CONFIG_DIR = Path.home() / ".config" / "vwcli"
CONFIG_FILE = CONFIG_DIR / "config"
BW_STALE_CIPHER_ERR = (
    "The client copy of this cipher is out of date. Resync the client and try again."
)
BW_SERVE_HOST = os.environ.get("BW_SERVE_HOST", "127.0.0.1")
BW_SERVE_PORT_BASE = int(os.environ.get("BW_SERVE_PORT_BASE", "18087"))
BW_SERVE_STARTUP_RETRIES = int(os.environ.get("BW_SERVE_STARTUP_RETRIES", "40"))
BW_SERVE_STARTUP_DELAY = float(os.environ.get("BW_SERVE_STARTUP_DELAY", "0.1"))
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CLONE_STRIP_KEYS = frozenset(
    {
        "id",
        "object",
        "revisionDate",
        "creationDate",
        "deletedDate",
        "passwordHistory",
        "attachments",
    }
)
