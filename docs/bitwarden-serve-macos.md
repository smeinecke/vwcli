# Bitwarden / Vaultwarden User Service (macOS)

This is the macOS counterpart of `bitwarden-serve/`. Since macOS has no
systemd, the persistent `bw serve` daemon runs as a **launchd LaunchAgent** -
the closest native equivalent: it starts at login, restarts on crash, and
relaunches when network state changes (e.g. VPN up).

The daemon binds a user-private Unix socket, so no TCP port is opened.
Install it before using vwcli regularly - without it every command pays
Node.js startup for a throwaway `bw serve`.

## Files

| File | Purpose |
|------|---------|
| `com.vwcli.bitwarden-serve.plist` | LaunchAgent template (paths substituted at install) |
| `start-bw-serve.sh` | Startup script (network reachability check + `bw serve`) |
| `install-bitwarden-serve.sh` | Installer - installs script + plist, creates env file, bootstraps agent |

## Prerequisites

```bash
brew install bitwarden-cli
bw --version
```

or install `bw` via nvm as on Linux. `bw` >= 2025.8 is required - that release
added native `unix://` `--hostname` support to `bw serve`
(bitwarden/clients#14262), which this setup relies on. The installer offers
both install paths when `bw` is missing.

## Installation

Run the installer once as the target user (or let `make install` run it):

```bash
./install-bitwarden-serve.sh
```

The installer will:

1. Check for `bw` and offer `brew install bitwarden-cli` (or nvm) when missing.
2. Install `start-bw-serve.sh` to `~/.local/bin/` and the rendered plist to
   `~/Library/LaunchAgents/com.vwcli.bitwarden-serve.plist`.
3. Create/update `~/.config/vwcli/bw-serve.env` with `BW_SESSION`,
   `BW_SESSION_EXPIRES` and `BW_SERVER_URL` (mode `0600`).
4. Bootstrap the LaunchAgent via `launchctl bootstrap gui/<uid>` (RunAtLoad
   starts it immediately).
5. Write `BW_SERVE_URL=unix://~/Library/Application Support/vwcli/bw.sock`
   to `~/.config/vwcli/config` so vwcli uses the daemon.

Re-run the installer at any time to update the script/plist without losing
the existing env file.

## Env file

Location: `~/.config/vwcli/bw-serve.env` (mode `0600`)

```bash
BW_SESSION=...        # session token for bw serve
BW_SESSION_EXPIRES=...# epoch, informational
BW_SERVER_URL=...     # vault server, used for the reachability check
BW_SERVE_SOCKET=...   # optional socket-path override
```

`vwcli login` updates `BW_SESSION`/`BW_SESSION_EXPIRES` here and kickstarts
the agent, exactly like it does for the systemd env file on Linux.

## Lifecycle

```bash
launchctl kickstart -k gui/$(id -u)/com.vwcli.bitwarden-serve   # restart
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.vwcli.bitwarden-serve.plist  # unload
launchctl print gui/$(id -u)/com.vwcli.bitwarden-serve          # status
```

Logs: `~/Library/Logs/vwcli/bitwarden-serve.log` (+ `.err.log`).

KeepAlive semantics: `Crashed` + `NetworkState` relaunch the daemon after a
crash or network transition; a clean exit (server unreachable) stays down so
it does not spin in a restart loop.

## Unix socket

`bw serve --hostname unix://<sock> --port 80` binds
`~/Library/Application Support/vwcli/bw.sock` (mode `0600` via umask).
`--port 80` is only used for bw's HTTP Host-header allowlist - no TCP port
is opened. No `SocketBindDeny` equivalent exists under launchd, but the
socket lives inside the user's own Library tree.

## How vwcli uses it

Resolution order inside vwcli:

1. `BW_SERVE_URL` from `~/.config/vwcli/config` - set to the unix socket by
   the installer.
2. If unreachable, the LaunchAgent check (`launchctl print`) + start
   (`bootstrap`/`kickstart`), then the socket path.
3. Otherwise a temporary `bw serve` per command (slow fallback).

`vwcli login` renews the session in the env file and restarts the agent, so
the daemon always holds a fresh `BW_SESSION` - same as the Linux flow.
