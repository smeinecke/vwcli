#!/usr/bin/env bash
set -euo pipefail

SERVER_URL="${BW_SERVER_URL:-}"

if [[ -n "$SERVER_URL" ]]; then
  URL_NO_SCHEME="${SERVER_URL#*://}"
  URL_HOSTPORT="${URL_NO_SCHEME%%/*}"
  URL_HOST="${URL_HOSTPORT%%:*}"
else
  echo "BW_SERVER_URL is not set; skipping backup." >&2
  exit 0
fi

BACKUP_DIR="$HOME/.local/share/bitwarden-backup"
STAMP="$(date +%F-%H%M%S)"
OUT="$BACKUP_DIR/vault-$STAMP.json"

run_bw() {
  if command -v bw >/dev/null 2>&1; then
    bw "$@"
    return
  fi

  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    # shellcheck source=/dev/null
    source "$NVM_DIR/nvm.sh"
  fi

  if type -t nvm >/dev/null 2>&1; then
    nvm exec --silent --lts -- bw "$@"
    return
  fi

  echo "Neither nvm nor bw was found. Install nvm+@bitwarden/cli or ensure bw is in PATH." >&2
  return 127
}

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# only run when server is reachable
if [[ ! "$URL_HOST" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  getent hosts "$URL_HOST" >/dev/null 2>&1 || exit 0
fi
curl -fsS --connect-timeout 5 "${SERVER_URL%/}/api/alive" >/dev/null 2>&1 || exit 0

# optional but sensible: refresh local cache first
run_bw sync

# requires BW_SESSION in environment and BW_EXPORT_PASSWORD in env file
run_bw export \
  --format encrypted_json \
  --password "$BW_EXPORT_PASSWORD" \
  --output "$OUT"

chmod 600 "$OUT"

# keep only latest 7 backups
ls -1t "$BACKUP_DIR"/vault-*.json | tail -n +8 | xargs -r rm -f
