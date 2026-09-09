# Bitwarden / Vaultwarden User Service

This directory contains a systemd user service that runs `bw serve` as a persistent local daemon, an optional scheduled encrypted-export backup, and a local Vaultwarden instance for fallback/testing.

## Files

| File | Purpose |
|------|---------|
| `bitwarden-cli.service` | Systemd user unit - runs `bw serve` |
| `bitwarden-export.service` | Systemd user unit - one-shot export/backup |
| `bitwarden-export.timer` | Systemd timer - triggers export every 6 h |
| `start-bw-serve.sh` | Startup script (network/VPN check + `bw serve`) |
| `bitwarden-backup.sh` | Backup script (sync + encrypted JSON export, keep last 7) |
| `install-bitwarden-cli-user-service.sh` | Installer - copies units & scripts, creates env file |
| `bw-unix-socket-patch.js` | Node.js `--require` patch that redirects `bw serve` from TCP to a Unix socket |
| `nm-dispatcher-bitwarden-vpn-up.sh` | NetworkManager dispatcher hook - starts service on VPN up |
| `docker-compose.yml` | Local Vaultwarden instance for testing |

## Prerequisites

### nvm + Node.js LTS

The scripts prefer the `bw` CLI managed via nvm, with a plain `bw` in `PATH` as fallback.

```bash
# Install nvm
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash

# Reload shell, then:
nvm install --lts
nvm alias default 'lts/*'

# Install Bitwarden CLI
npm install -g @bitwarden/cli
bw --version
```

Alternatively, install `bw` via your package manager or download the binary and place it in `PATH`.

## Installation

Run the installer **once** as the target user:

```bash
./install-bitwarden-cli-user-service.sh
```

The installer will:
1. Check for the Bitwarden CLI (`bw`) and optionally install it (and `nvm`) only when missing.
2. Stop any running `bitwarden-cli.service` (and `bitwarden-export.timer` only when backup install is requested) during the update.
3. Copy service units to `~/.config/systemd/user/`, scripts to `~/.local/bin/`, and the socket patch to `~/.local/lib/`.
4. Create/update `~/.config/systemd/user/bitwarden-cli.env` with `BW_SESSION` and `BW_SERVER_URL`.
5. Enable and start `bitwarden-cli.service`.

Backup install is opt-in:

```bash
./install-bitwarden-cli-user-service.sh --install-backup
```

This additionally installs/enables `bitwarden-export.service` and `bitwarden-export.timer`, and ensures `BW_EXPORT_PASSWORD` is present.

VPN-aware start is also opt-in:

```bash
./install-bitwarden-cli-user-service.sh --install-vpn-hook
```

This installs a NetworkManager dispatcher hook (to `/etc/NetworkManager/dispatcher.d/`) that automatically starts `bitwarden-cli.service` for every active user session whenever a VPN connection comes up. It requires `sudo`.

Re-run the installer at any time to update scripts/units without losing the existing env file.

## Env file

Location: `~/.config/systemd/user/bitwarden-cli.env`

```ini
BW_SESSION=<token from bw unlock --raw>
BW_SESSION_EXPIRES=<Unix timestamp of token expiry>
BW_EXPORT_PASSWORD=<passphrase for encrypted JSON exports>
BW_SERVER_URL=<vaultwarden base URL, e.g. https://vault.example.com>
```

- `BW_SESSION` is consumed by `bw serve` and `bw export`.
- `BW_SESSION_EXPIRES` is written by the installer and by `vwcli login`; `vwcli` reads it to detect expiry and auto-renew before it hits the service.
- `BW_EXPORT_PASSWORD` is consumed only by `bitwarden-backup.sh` (needed only when backup is installed).
- `BW_SERVER_URL` is consumed by `start-bw-serve.sh` and `bitwarden-backup.sh` for DNS and endpoint reachability checks. The hostname is derived from the URL at runtime.

If `.vault_pass.txt` exists at the repo root it is read automatically as `BW_EXPORT_PASSWORD` during install.
If not present, the installer prompts for `BW_EXPORT_PASSWORD`.

**Permissions:** the installer creates this file with mode `0600` (owner-read only).

To override the Vaultwarden target, edit:

```ini
BW_SERVER_URL=https://<your-hostname>
```

Then restart:

```bash
systemctl --user restart bitwarden-cli.service
```

## Session token renewal

`BW_SESSION` tokens are backed by Vaultwarden's idle refresh token, which defaults to 30 days. A conservative TTL of 29 days is used here.

### Automatic renewal via `vwcli`

The installer records `BW_SESSION_EXPIRES` (Unix timestamp) in both the vwcli config and the systemd env file. Before each `vwcli` operation, `ensure_session` checks expiry and attempts a silent `bw unlock --raw`. On success it:

1. Updates `~/.config/vwcli/config` (`BW_SESSION` + `BW_SESSION_EXPIRES`).
2. Updates `~/.config/systemd/user/bitwarden-cli.env` with the new token.
3. Restarts `bitwarden-cli.service` so the running daemon uses the fresh token.

The silent re-unlock works as long as the vault master password is held in the kernel keyring (i.e. the user session is still active). If it fails, `vwcli` prompts to run `vwcli login` interactively.

`vwcli login` always does a full unlock and propagates the token to both files + restarts the service.

The TTL can be tuned via the `BW_SESSION_TTL_SECONDS` environment variable (default: `2505600`).

### Manual renewal (fallback)

```bash
vwcli login
```

## Service lifecycle

```bash
# Status (omit bitwarden-export.timer if backup was not installed)
systemctl --user status bitwarden-cli.service bitwarden-export.timer

# Logs
journalctl --user -u bitwarden-cli.service -f
journalctl --user -u bitwarden-export.service

# Manual backup run
systemctl --user start bitwarden-export.service

# Restart serve daemon
systemctl --user restart bitwarden-cli.service

# Disable everything
systemctl --user disable --now bitwarden-cli.service bitwarden-export.timer
```

## Backup details

- Script: `bitwarden-backup.sh` (installed only with `--install-backup`)
- Output dir: `~/.local/share/bitwarden-backup/`
- File pattern: `vault-YYYY-MM-DD-HHMMSS.json`
- Format: Bitwarden encrypted JSON (AES-256 + the `BW_EXPORT_PASSWORD` passphrase)
- Retention: latest 7 files kept; older ones deleted automatically
- Timer schedule: 5 min after boot, then every 6 h while the machine is running
- The script exits silently (code 0) when the VPN / Bitwarden server is unreachable, so the timer never shows a failed state on disconnected machines.
- Scope: export contains user-owned vault entries only. Organization/company-owned items are not included.

## Unix socket mode

`bw serve` normally binds a TCP port (`127.0.0.1:8087`). The service instead uses `bw-unix-socket-patch.js` — a Node.js `--require` preload that intercepts `net.Server.prototype.listen` and redirects the bind to a Unix domain socket (`/run/user/<uid>/bw.sock`, mode `0600`).

Benefits: the socket is only accessible by the owning user, never reachable over the network, and disappears automatically when the service stops.

The socket path is controlled by the `BW_SERVE_SOCKET` environment variable (set in the service unit). The service also enforces `SocketBindDeny=tcp` so `bw serve` cannot fall back to a TCP listener even if the patch is absent or fails to load.

Manual test (requires `BW_SESSION` to be set and vault unlocked):

```bash
SOCK="/run/user/$(id -u)/bw.sock"
curl -s --unix-socket "$SOCK" http://localhost/status | jq .
```

## vwcli integration

The installer automatically writes `BW_SERVE_URL=unix:///run/user/<uid>/bw.sock` to `~/.config/vwcli/config`. `vwcli` parses the `unix://` scheme and connects via the domain socket instead of TCP - no manual configuration is needed.

## Integration testing

The repository includes an end-to-end integration test harness under `tests/integration/`.

It will:

1. Start an ephemeral Vaultwarden container on `https://127.0.0.1:18443` using a self-signed certificate.
2. Register a hardcoded demo user.
3. Run `bw login` against the container.
4. Start `bw serve` on a free local TCP port.
5. Exercise `vwcli create`, `search`, `update` and `delete` through `bw serve`.

Run the integration tests locally with:

```bash
make integration-test
```

Requirements: `docker compose`, `openssl`, `bw` in `PATH`, and Node.js for `bw`.
