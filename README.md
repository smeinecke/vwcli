# vwcli

Bitwarden/Vaultwarden CLI wrapper - a convenience layer on top of the official
`bw` CLI and its `bw serve` REST API, aimed at self-hosted Vaultwarden servers.

## Features

- Search, create, update, clone, delete and bulk-move vault items
- Transparent `bw serve` integration: reuses a running daemon (systemd user
  service or `BW_SERVE_URL`) when available, otherwise starts a throwaway
  `bw serve` per invocation - with automatic fallback to plain `bw` CLI calls
  when a REST endpoint is missing or fails
- Session management: caches `BW_SESSION` in `~/.config/vwcli/config`, tracks
  expiry and silently re-unlocks before it lapses (see
  [docs/configuration.md](docs/configuration.md))
- Hierarchical collections: local cache with `Parent/Child` paths, nested
  create/move/delete, bulk item moves between collections
- Attachments: list, upload and delete
- URI match types (`base_domain`, `host`, `starts_with`, `exact`, `regexp`,
  `never`) and generated passwords (24 chars, mixed)
- Optional `ansible-vault encrypt-string` output for passwords
  (`--to-ansible-vault`)
- `--dry-run` on write commands to preview the JSON payload before touching
  the vault

## Requirements

- Python >= 3.11
- Bitwarden CLI (`bw`) in `PATH`, or installed under nvm (`~/.nvm`) - vwcli
  falls back to `nvm exec --lts -- bw` automatically
- A Bitwarden-compatible server (Vaultwarden or bitwarden.com). For
  Vaultwarden, point `bw` at it once:

  ```bash
  bw config server https://vault.example.com
  ```

- A running `bw serve` daemon. vwcli is built around it - without one, every
  command spawns a throwaway instance and pays seconds of Node.js startup.
  The `bitwarden-serve/` systemd user service is the supported setup
  (socket-activated, hardened unit):

  ```bash
  bitwarden-serve/install-bitwarden-cli-user-service.sh
  ```

  Details: [docs/bitwarden-serve.md](docs/bitwarden-serve.md)

Optional:

- `ansible-vault` (only for `--to-ansible-vault`)
- the scheduled encrypted-export timer in `bitwarden-serve/`

## Install

```bash
# From the repo, into ~/.local/bin via uv:
uv tool install .

# Or run without installing:
uv sync
uv run vwcli help
```

## Quickstart

```bash
vwcli login                  # unlock vault, cache + propagate BW_SESSION
vwcli collections cache      # build the local collection-name cache
vwcli github                 # shorthand for `vwcli search github`

vwcli create --name example.com --username me@example.com \
    --generate-password --uri https://example.com::base_domain
```

See [QUICKSTART.md](QUICKSTART.md) for a guided tour and
[docs/commands.md](docs/commands.md) for the full command reference.

## Commands

| Command | Purpose |
|---------|---------|
| `vwcli login` | Unlock vault; cache `BW_SESSION` and propagate it to the systemd env file |
| `vwcli search <query>` | Search items (default command - `vwcli github` works too); `--json`, `--limit`, `--uri`, `--to-ansible-vault` |
| `vwcli create` | Create a login item; `--name`, `--username`, `--password`/`--generate-password`, `--notes`, `--uri`, `--vault`, `--collection`, `--organization-id`, `--dry-run` |
| `vwcli update` | Update an item selected by `--id` or `--search`; same field flags as create plus `--add-uri`/`--remove-uri`, `--clone`, `--dry-run` |
| `vwcli clone` | Alias for `update --clone` - duplicate an item, then apply edits to the copy |
| `vwcli delete` | Move an item to trash; `--id`/`--search`, `--yes` |
| `vwcli move` | Bulk-move items between collections; `--from`, `--to`, `--search`, `--dry-run`, `--yes` |
| `vwcli attachment` | `list` (default), `add --file`, `delete --attachment-id` |
| `vwcli collections` | `cache`, `list`, `search`, `add`, `update`, `delete`, `move` - manage org collections incl. `Parent/Child` nesting |
| `vwcli help` | Show help |

## Configuration

| File | Mode | Contents |
|------|------|----------|
| `~/.config/vwcli/config` | `0600` | `BW_SESSION`, `BW_SESSION_EXPIRES`, `BW_SERVE_URL` |
| `~/.cache/vwcli/collections.json` | - | Collection-name cache (`vwcli collections cache`) |
| `~/.config/systemd/user/bitwarden-cli.env` | `0600` | Service env file written by the `bitwarden-serve/` installer; updated on login/renewal |

Key environment variables: `BW_SESSION`, `BW_SERVE_URL`
(e.g. `unix:///run/user/<uid>/bw.sock`), `BW_PASSWORD` (non-interactive
unlock), `BW_SESSION_TTL_SECONDS`. Full list:
[docs/configuration.md](docs/configuration.md).

## Documentation

- [QUICKSTART.md](QUICKSTART.md) - first-time setup walkthrough
- [docs/commands.md](docs/commands.md) - full command and flag reference
- [docs/configuration.md](docs/configuration.md) - config files, env vars, session lifecycle
- [docs/architecture.md](docs/architecture.md) - how vwcli drives `bw`/`bw serve`, fallbacks, caching
- [docs/development.md](docs/development.md) - lint/test setup, CI, repo layout
- [docs/bitwarden-serve.md](docs/bitwarden-serve.md) - systemd user service, Unix socket activation, backups

## Development

```bash
uv sync                 # install dev dependencies
make validate           # ruff format+lint, bandit, pyright, vulture, xenon
make test               # unit tests
make integration-test   # Vaultwarden container + bw serve (needs docker compose + bw)
```

## License

MIT
