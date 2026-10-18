# AGENTS.md

Guidance for agents working in this repository.

## Project

`vwcli` - a Bitwarden/Vaultwarden CLI wrapper (Python >= 3.11, single runtime
dependency: `rich`). Source lives in `src/vwcli/`; see `docs/architecture.md`
for how it drives `bw`/`bw serve`.

## Commands

```bash
uv sync                 # install deps (uv-managed .venv)
make test               # unit tests (integration marker deselected)
make integration-test   # needs docker compose + bw + node
make validate           # ruff format+check, bandit, pyright, vulture, xenon
make fix                # ruff format + autofix
```

Always run `make validate` and `make test` before committing. CI additionally
runs `make integration-test` on PRs (`.github/workflows/`).

## Conventions

- Ruff: line length 160, double quotes; `docs/` and `tests/` are lint-excluded.
- `from __future__ import annotations` at the top of every module.
- Errors: raise `VwcliError` (empty message means stderr was already written);
  never print secrets/session tokens.
- Every `bw_*` method in `client.py` tries the `bw serve` REST endpoint first
  and falls back to the `bw` CLI subprocess - preserve that pattern.
- Use `-` instead of em dashes in comments and docs.
- `uv.lock` is committed - update it via `uv`, not by hand.
- Docs: `README.md`, `QUICKSTART.md`, `AGENTS.md` at root; everything else in
  `docs/` (including `docs/bitwarden-serve.md`, moved out of `bitwarden-serve/`).

## Gotchas

- Integration tests run against `vaultwarden/server:latest` - upstream API
  changes have broken the harness before. Registration is two-step since
  1.37.4 (`send-verification-email` -> `register/finish`, legacy endpoint
  kept as fallback). 1.37.4 also rejects org-cipher creation via
  `POST /ciphers` (`encryptedByKeyId` is checked against the user key), so
  vwcli creates org-bound items as personal first and shares them into the
  org - the share endpoint is exempt. Preserve that flow.
- The collection cache (`~/.cache/vwcli/collections.json`) stores full
  `Parent/Child` paths; `--collection` resolution needs a unique exact match.
- `vwcli <unknown>` is shorthand for `vwcli search <unknown>` - keep new
  subcommand names out of collision with typical search terms.
- `Config` resolves `Path.home()` at construction, not import - the module
  constants would freeze the real HOME and let tests clobber the real
  `~/.config/vwcli/config`. Keep path resolution dynamic.
