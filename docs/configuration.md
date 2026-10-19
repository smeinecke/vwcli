# Configuration

## Files

| Path | Mode | Contents |
|------|------|----------|
| `~/.config/vwcli/config` | `0600` | `KEY=value` lines: `BW_SESSION`, `BW_SESSION_EXPIRES`, `BW_SERVE_URL` |
| `~/.cache/vwcli/collections.json` | - | Collection cache written by `vwcli collections cache` |
| `~/.config/systemd/user/bitwarden-cli.env` | `0600` | Env file for the `bitwarden-serve` systemd unit (created by its installer, updated by `vwcli login`) |

All files are created on demand; the config dir/file are chmod `0700`/`0600`.

### `~/.config/vwcli/config`

Simple `KEY=value` file (comments with `#` allowed). Recognized keys:

| Key | Written by | Meaning |
|-----|-----------|---------|
| `BW_SESSION` | `vwcli login` / auto-renew | Session token used for all vault operations |
| `BW_SESSION_EXPIRES` | `vwcli login` / auto-renew | Unix timestamp after which the token is considered stale |
| `BW_SERVE_URL` | installer or manually | Preferred `bw serve` endpoint, e.g. `unix:///run/user/<uid>/bw.sock` or `http://127.0.0.1:8087` |

Environment variables of the same name take precedence over file values.

### `~/.cache/vwcli/collections.json`

JSON list of `{id, name, organizationId, parentId}` where `name` is the full
path (`Parent/Child/Leaf`) computed from `parentId` chains. Rebuild with
`vwcli collections cache` or `--refresh-cache` on supported commands.

## Environment variables

| Variable | Default | Used for |
|----------|---------|----------|
| `BW_SESSION` | - | Session token (also read from the config file) |
| `BW_PASSWORD` | - | Master password env for `bw unlock --passwordenv` during `vwcli login` |
| `BW_SERVE_URL` | - | Preferred `bw serve` endpoint (`http://...` or `unix:///path.sock`) |
| `BW_SERVE_SOCKET` | `/run/user/<uid>/bw.sock` | Socket path used when connecting via the systemd service |
| `BW_SERVE_HOST` | `127.0.0.1` | Bind host for a self-spawned `bw serve` |
| `BW_SERVE_STARTUP_RETRIES` | `40` | Readiness poll count for a self-spawned `bw serve` |
| `BW_SERVE_STARTUP_DELAY` | `0.1` | Seconds between readiness polls |
| `BW_SESSION_TTL_SECONDS` | `2505600` (29 days) | Assumed session lifetime when stamping `BW_SESSION_EXPIRES` |
| `NVM_DIR` | `~/.nvm` | Location of nvm for the `bw` fallback (`nvm exec --lts -- bw`) |
| `XDG_CONFIG_HOME` | `~/.config` | Base dir for the systemd env file lookup |

## Session lifecycle

`vwcli login` performs a full unlock and calls `_apply_new_session`, which:

1. Stamps `BW_SESSION` + `BW_SESSION_EXPIRES` (now + `BW_SESSION_TTL_SECONDS`)
   into `~/.config/vwcli/config`.
2. If `bitwarden-cli.service` is installed, updates
   `~/.config/systemd/user/bitwarden-cli.env` and restarts the service so the
   daemon picks up the new token.

Before every vault operation, `ensure_session` checks the cached expiry. When
expired it attempts a silent `bw unlock --raw` with stdin on `/dev/null`
(non-interactive - works while the master password is still held by the
desktop keyring). On success the new token is propagated exactly like a
`login`; on failure the command tells you to run `vwcli login` interactively.

The 29-day default TTL is conservative relative to Vaultwarden's 30-day
refresh-token idle timeout.

## How `bw serve` is selected

For commands that support it, `bw_serve_url` is resolved in this order:

1. **`BW_SERVE_URL` (env or config file)** - used when it answers `/status`.
2. **systemd user service** (Linux only) - if `bitwarden-cli.service` is
   installed it is started if needed and vwcli talks to it over the Unix
   socket (`unix://$BW_SERVE_SOCKET`).
3. **Self-spawned daemon** - `bw serve --hostname 127.0.0.1 --port <free>`
   started for the duration of the command and terminated on exit. This is a
   compatibility fallback for one-off/CI use, not a daily-driver mode: the
   startup cost makes interactive use painfully slow, so vwcli warns and
   points at the `bitwarden-serve/` installer instead.

If no `bw` binary is in `PATH`, vwcli runs it through nvm
(`nvm exec --silent --lts -- bw`); if nvm is missing too, commands fail with
"Missing required command: bw".
