# Development

## Setup

```bash
uv sync    # creates .venv with all dev dependencies
```

Requires Python >= 3.11. Dev tooling (pytest, ruff, pyright, vulture, xenon,
bandit, pre-commit, ansible-core) lives in the `dev` dependency group in
`pyproject.toml`. `uv.lock` is committed (application, not a library).

## Repo layout

```
src/vwcli/              # package (cli.py, client.py, config.py, constants.py, output.py)
tests/test_vwcli.py     # unit tests (no external services)
tests/integration/      # Vaultwarden docker harness + bw serve tests
tests/e2e/              # privileged systemd-container E2E for the user units
bitwarden-serve/        # systemd units, installer, scripts, Node socket patch
docs/                   # this documentation
.github/workflows/      # check.yml, integration.yml, e2e-systemd.yml
```

## Make targets

| Target | What it runs |
|--------|--------------|
| `make format` | `ruff format --check --diff` |
| `make reformat-ruff` | `ruff format` (writes) |
| `make check` | `ruff check` |
| `make fix-ruff` | `ruff check --fix` |
| `make fix` | reformat + autofix |
| `make test` | `pytest tests` (unit tests; `-m "not integration"`) |
| `make test-cov` | pytest with coverage report |
| `make integration-test` | integration tests, excluding `@pytest.mark.slow` |
| `make integration-test-all` | all integration tests incl. the slow CLI-fallback mode |
| `make vulture` | dead-code scan |
| `make xenon` | cyclomatic complexity (block D, max module B, avg B) |
| `make bandit` | security lint |
| `make pyright` | type check |
| `make validate` | format + check + bandit + pyright + vulture + xenon |
| `make all` | validate + test |

Ruff config: line length 160, double quotes; `docs/` and `tests/` are excluded
from lint. Pre-commit hooks (pre-push stage) run ruff + ruff-format + pyright.

## Tests

### Unit tests

```bash
make test
```

Pure pytest, no external services. `pyproject.toml` sets
`--import-mode=importlib` and deselects the `integration` marker by default.

### Integration tests

```bash
make integration-test       # TCP + Unix-socket bw serve modes
make integration-test-all   # + slow self-spawned fallback mode
```

The harness (`tests/integration/conftest.py`):

1. Generates a self-signed TLS cert and starts `vaultwarden/server:latest`
   on `https://127.0.0.1:18443` via docker compose (`I_REALLY_WANT_VOLATILE_STORAGE`,
   `WEB_VAULT_ENABLED=false`, `SIGNUPS_ALLOWED=true`).
2. Registers a test user through the two-step registration API
   (`/identity/accounts/register/send-verification-email` ->
   `/identity/accounts/register/finish`; the legacy single-shot endpoint is
   used as a fallback for older images).
3. Logs in with `bw login` and starts `bw serve` in three modes: TCP port,
   Unix socket (`--hostname unix://...`) and `systemd-socket-activate`
   (`--hostname fd+listening://3`, exercises `LISTEN_FDS`). Requires
   `bw` >= 2025.8 for the native socket support.
4. Exercises the full vwcli surface: create/search/update/clone/delete,
   attachments, collections, move, ansible-vault output.

Requirements: `docker compose`, `openssl`, `bw` in `PATH`, Node.js.
`NODE_TLS_REJECT_UNAUTHORIZED=0` is set inside the harness for the
self-signed cert.

### E2E (systemd socket activation)

`tests/e2e/` builds a privileged systemd Docker container, installs the real
`bitwarden-cli.socket`/`bitwarden-cli.service` units and verifies `bw serve`
answers over `/run/user/0/bw.sock` with no TCP listener. Runs as root's user
manager (the only manager with the capabilities the unit's sandbox needs).

```bash
E2E_SEED_DIR=/tmp/vwcli-e2e-seed uv run python -m tests.e2e.prepare_session
docker build -t vwcli-e2e -f tests/e2e/Dockerfile tests/e2e
docker run -d --name vwcli-e2e --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  -v "$PWD:/src:ro" -v /tmp/vwcli-e2e-seed:/seed:ro \
  vwcli-e2e
docker exec vwcli-e2e bash /src/tests/e2e/run-e2e.sh
```

## CI

| Workflow | Trigger | Job |
|----------|---------|-----|
| `check.yml` | PR, push, manual | Unit tests + full validate chain + coverage artifact |
| `integration.yml` | PR, push to main, manual | Integration suite (installs `@bitwarden/cli` via npm) |
| `e2e-systemd.yml` | manual (`workflow_dispatch`) | systemd socket-activation E2E |

## Conventions

- Keep comments free of em dashes (use `-`).
- Match the existing style: `from __future__ import annotations`, ruff
  formatting, minimal comments.
- Write-path errors go through `VwcliError` (empty message = error already
  printed to stderr); user-facing output via `print`/Rich tables.
- When touching `bw serve` routes, keep the CLI fallback path working -
  every `bw_*` wrapper in `client.py` tries REST first and degrades to a
  `bw` subprocess.
