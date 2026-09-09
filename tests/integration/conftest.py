from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.client
import os
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
from vaultwarden.utils.crypto import make_asym_key, make_master_key, make_sym_key


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
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_tcp)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    return vaultwarden_server.home


@pytest.fixture()
def integration_env_unix(
    vaultwarden_server: VaultwardenServer,
    bw_serve_url_unix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide env for an integration test using the Unix socket bw serve."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.setenv("BW_SERVE_URL", bw_serve_url_unix)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    return vaultwarden_server.home


@pytest.fixture()
def integration_env_fallback(
    vaultwarden_server: VaultwardenServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide env for an integration test that falls back to bw CLI commands."""
    monkeypatch.setenv("HOME", str(vaultwarden_server.home))
    monkeypatch.setenv("BW_SESSION", vaultwarden_server.session)
    monkeypatch.delenv("BW_SERVE_URL", raising=False)
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    return vaultwarden_server.home
