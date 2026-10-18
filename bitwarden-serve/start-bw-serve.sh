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

# Unix socket mode: bw serve (>= 2025.8, bitwarden/clients#14262) accepts
# non-TCP listeners via --hostname:
#   fd+listening://<fd> - accept() on an already-bound socket handed over by
#                         the supervisor. Under bitwarden-cli.socket, systemd
#                         binds /run/user/<uid>/bw.sock and passes it as fd 3
#                         (LISTEN_FDS=1, LISTEN_PID=$$), so bw never calls
#                         bind() and the unit can run with SocketBindDeny=any.
#   unix://<path>       - bind a Unix socket directly (standalone runs).
# --port 80 is required even though no TCP bind happens: bw validates the
# HTTP Host header against the configured port and only allows bare
# "localhost"/"127.0.0.1" (what unix-socket clients send) for port 80/443.
if [[ "${LISTEN_PID:-}" == "$$" && "${LISTEN_FDS:-0}" -ge 1 ]]; then
  SERVE_ARGS=(serve --hostname "fd+listening://3" --port 80)
else
  BW_SERVE_SOCKET="${BW_SERVE_SOCKET:-/run/user/$(id -u)/bw.sock}"
  if [[ -S "$BW_SERVE_SOCKET" ]]; then
    rm -f "$BW_SERVE_SOCKET"
  fi
  umask 077 # unix:// listeners get their mode from umask; keep it 0600
  SERVE_ARGS=(serve --hostname "unix://$BW_SERVE_SOCKET" --port 80)
fi

# Resolve the nvm-managed bin dir and exec node on the bw entrypoint directly.
# (The `bw` shim is a `#!/usr/bin/env node` script, which would fail here
# because node is not in the service's default PATH.)
if type -t nvm >/dev/null 2>&1; then
  NVM_NODE_BIN="$(dirname "$(nvm which 'lts/*' 2>/dev/null || true)")"
  if [[ "$NVM_NODE_BIN" == */bin && -x "$NVM_NODE_BIN/bw" && -x "$NVM_NODE_BIN/node" ]]; then
    exec "$NVM_NODE_BIN/node" "$NVM_NODE_BIN/bw" "${SERVE_ARGS[@]}"
  fi
  nvm exec --silent --lts -- bw "${SERVE_ARGS[@]}"
  exit $?
fi

if command -v bw >/dev/null 2>&1; then
  exec bw "${SERVE_ARGS[@]}"
fi

echo "Neither nvm nor bw was found. Install nvm+@bitwarden/cli or ensure bw is in PATH." >&2
exit 127
