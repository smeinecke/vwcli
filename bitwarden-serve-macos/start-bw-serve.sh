#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${BW_SERVE_ENV_FILE:-$HOME/.config/vwcli/bw-serve.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

SERVER_URL="${BW_SERVER_URL:-}"

if [[ -n "$SERVER_URL" ]]; then
  # Poll until the endpoint answers (covers VPN or network still coming up).
  ready=0
  for ((i = 0; i < 30; i++)); do
    if curl -fsS --connect-timeout 5 "${SERVER_URL%/}/api/alive" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  if (( ! ready )); then
    echo "Bitwarden endpoint not reachable yet: ${SERVER_URL%/}"
    exit 0
  fi
else
  echo "BW_SERVER_URL is not set; starting bw serve without remote reachability check." >&2
fi

BW_SERVE_SOCKET="${BW_SERVE_SOCKET:-$HOME/Library/Application Support/vwcli/bw.sock}"
mkdir -p "$(dirname "$BW_SERVE_SOCKET")"
if [[ -S "$BW_SERVE_SOCKET" ]]; then
  rm -f "$BW_SERVE_SOCKET"
fi
umask 077 # unix:// listeners get their mode from umask; keep it 0600

# launchd runs with a minimal PATH, so probe the usual install locations
# first, then PATH, then an nvm-managed bw.
for bw_bin in "/opt/homebrew/bin/bw" "/usr/local/bin/bw"; do
  if [[ -x "$bw_bin" ]]; then
    exec "$bw_bin" serve --hostname "unix://$BW_SERVE_SOCKET" --port 80
  fi
done

if command -v bw >/dev/null 2>&1; then
  exec bw serve --hostname "unix://$BW_SERVE_SOCKET" --port 80
fi

NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
fi

if type -t nvm >/dev/null 2>&1; then
  NVM_NODE_BIN="$(dirname "$(nvm which 'lts/*' 2>/dev/null || true)")"
  if [[ "$NVM_NODE_BIN" == */bin && -x "$NVM_NODE_BIN/bw" && -x "$NVM_NODE_BIN/node" ]]; then
    exec "$NVM_NODE_BIN/node" "$NVM_NODE_BIN/bw" serve --hostname "unix://$BW_SERVE_SOCKET" --port 80
  fi
fi

echo "bw was not found. Install it via 'brew install bitwarden-cli' or nvm + @bitwarden/cli." >&2
exit 127
