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

- Integration tests pull `vaultwarden/server:latest` - upstream API changes
  can break the harness (e.g. registration moved to a two-step
  `send-verification-email` -> `register/finish` flow in 1.37.x).
- The collection cache (`~/.cache/vwcli/collections.json`) stores full
  `Parent/Child` paths; `--collection` resolution needs a unique exact match.
- `vwcli <unknown>` is shorthand for `vwcli search <unknown>` - keep new
  subcommand names out of collision with typical search terms.
