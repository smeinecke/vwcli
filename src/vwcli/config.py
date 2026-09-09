from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from . import constants


def safe_chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


class Config:
    def __init__(self) -> None:
        self.config_dir = constants.CONFIG_DIR
        self.config_file = constants.CONFIG_FILE
        self.cache_dir = constants.CACHE_DIR
        self.collection_cache = constants.COLLECTION_CACHE
        self.ensure_dir()

    @staticmethod
    def _ensure_secure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        safe_chmod(path, 0o700)

    def ensure_dir(self) -> None:
        self._ensure_secure_dir(self.config_dir)
        if not self.config_file.exists():
            self.config_file.touch()
            safe_chmod(self.config_file, 0o600)

    def load(self, client: Any) -> None:
        self.ensure_dir()
        for line in self.config_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key == "BW_SESSION" and not client.bw_session and value:
                client.bw_session = value
                os.environ["BW_SESSION"] = value
            elif key == "BW_SESSION_EXPIRES" and not client.bw_session_expires and value:
                with contextlib.suppress(ValueError):
                    client.bw_session_expires = int(value)
            elif key == "BW_SERVE_URL" and not client.bw_serve_url and value:
                client.bw_serve_url = value

    def set(self, key: str, value: str) -> None:
        self.ensure_dir()
        lines = self.config_file.read_text(encoding="utf-8").splitlines()
        out_lines: list[str] = []
        replaced = False

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                out_lines.append(line)
                continue
            file_key = line.split("=", 1)[0].strip()
            if file_key == key:
                out_lines.append(f"{key}={value}")
                replaced = True
            else:
                out_lines.append(line)

        if not replaced:
            out_lines.append(f"{key}={value}")

        self.config_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        safe_chmod(self.config_file, 0o600)

    def ensure_cache_dir(self) -> None:
        self._ensure_secure_dir(self.cache_dir)
