#!/usr/bin/env python3
"""Prepare a real bw session and Bitwarden CLI data dir for the systemd E2E.

Starts the integration Vaultwarden container, registers a user, runs
`bw login`, and copies the resulting `~/.config/Bitwarden CLI` directory plus
the BW_SESSION value into a seed directory that the E2E container can mount.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.integration.conftest import (
    _bw_login,
    _generate_certs,
    _register_user,
    _wait_for_vaultwarden,
)

TEST_EMAIL = "e2e-test@example.com"
TEST_PASSWORD = "TestP@ssw0rd!"
TEST_HOST = "127.0.0.1"
TEST_PORT = 18443


def main() -> int:
    seed_dir = Path(os.environ.get("E2E_SEED_DIR", "/tmp/vwcli-e2e-seed"))
    seed_dir.mkdir(parents=True, exist_ok=True)

    compose_file = Path(__file__).parents[1] / "integration" / "docker-compose.yml"
    tls_dir = seed_dir / "tls"
    _generate_certs(tls_dir)

    env = os.environ.copy()
    env["TLS_DIR"] = str(tls_dir)

    # Make sure no stale Vaultwarden container is running.
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", "vwcli-e2e", "down", "-v"],
        env=env,
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", "vwcli-e2e", "up", "-d"],
        env=env,
        check=True,
    )

    url = f"https://{TEST_HOST}:{TEST_PORT}"
    try:
        _wait_for_vaultwarden(url)
        bw_home = seed_dir / "bw-home"
        bw_home.mkdir(parents=True, exist_ok=True)
        _register_user(url, TEST_EMAIL, TEST_PASSWORD)
        session = _bw_login(url, TEST_EMAIL, TEST_PASSWORD, bw_home)

        (seed_dir / "session.env").write_text(f"BW_SESSION={session}\n", encoding="utf-8")
        (seed_dir / "email.txt").write_text(TEST_EMAIL, encoding="utf-8")
        (seed_dir / "password.txt").write_text(TEST_PASSWORD, encoding="utf-8")

        bw_config = bw_home / ".config" / "Bitwarden CLI"
        if not bw_config.exists():
            print("No Bitwarden CLI data dir after login", file=sys.stderr)
            return 1
        shutil.copytree(bw_config, seed_dir / "Bitwarden CLI", dirs_exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Session preparation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Session prepared in {seed_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
