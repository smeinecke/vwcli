#!/usr/bin/env bash
set -euo pipefail

SERVER_URL="${BW_SERVER_URL:-}"

if [[ -n "$SERVER_URL" ]]; then
  URL_NO_SCHEME="${SERVER_URL#*://}"
  URL_HOSTPORT="${URL_NO_SCHEME%%/*}"
  URL_HOST="${URL_HOSTPORT%%:*}"

  # 1) Wait until NetworkManager says some connection is up
  /usr/bin/nm-online -q --timeout=30 || exit 0

  # 2) Wait until the target host resolves (skip for IPv4 literals)
  if [[ ! "$URL_HOST" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    for ((i = 0; i < 15; i++)); do
      if /usr/bin/getent hosts "$URL_HOST" >/dev/null 2>&1; then
        break
      fi
      if ((i == 14)); then
        echo "Bitwarden hostname not resolvable yet: $URL_HOST"
        exit 0
      fi
      sleep 2
    done
  fi

  # 3) verify endpoint is reachable before starting
  if ! /usr/bin/curl -fsS --connect-timeout 5 "${SERVER_URL%/}/api/alive" >/dev/null 2>&1; then
    echo "Bitwarden endpoint not reachable yet: ${SERVER_URL%/}"
    exit 0
  fi
else
  echo "BW_SERVER_URL is not set; starting bw serve without remote reachability check." >&2
fi

# 4) Start bw serve. Prefer nvm-managed runtime, fallback to bw in PATH.
NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
fi

# Unix socket mode: patch Node's net.Server.listen so the HTTP API is served
# on a Unix socket instead of a TCP port. When started through
# bitwarden-cli.socket, systemd has already bound the socket and hands it
# over as fd 3 (LISTEN_FDS); the patch listens on that fd and never calls
# bind(), which lets the unit run with SocketBindDeny=any. When run
# standalone, the patch binds BW_SERVE_SOCKET itself instead.
BW_SERVE_SOCKET="${BW_SERVE_SOCKET:-/run/user/$(id -u)/bw.sock}"
export BW_SERVE_SOCKET
PATCH_JS="${PATCH_JS:-$HOME/.local/lib/bw-unix-socket-patch.js}"
if [[ -f "$PATCH_JS" ]]; then
  export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require=$PATCH_JS"
fi

# Socket activation identifies the fd-owning process via LISTEN_PID, so the
# final hop must exec() bw to keep the same PID. `nvm exec` would fork a
# subshell and break that check, so resolve the nvm-managed bin dir and exec
# node on the bw entrypoint directly. (The `bw` shim is a `#!/usr/bin/env node`
# script, which would additionally fail here because node is not in the
# service's default PATH.)
if type -t nvm >/dev/null 2>&1; then
  NVM_NODE_BIN="$(dirname "$(nvm which 'lts/*' 2>/dev/null || true)")"
  if [[ "$NVM_NODE_BIN" == */bin && -x "$NVM_NODE_BIN/bw" && -x "$NVM_NODE_BIN/node" ]]; then
    exec "$NVM_NODE_BIN/node" "$NVM_NODE_BIN/bw" serve
  fi
  nvm exec --silent --lts -- bw serve
  exit $?
fi

if command -v bw >/dev/null 2>&1; then
  exec bw serve
fi

echo "Neither nvm nor bw was found. Install nvm+@bitwarden/cli or ensure bw is in PATH." >&2
exit 127
