#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
ENV_FILE="${ENV_FILE:-$XDG_CONFIG/systemd/user/bitwarden-cli.env}"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bitwarden-serve"
STATE_FILE="${STATE_FILE:-$STATE_DIR/vw-fallback.state}"

HOST="${HOST:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/share/bitwarden-backup}"
EXPORT_FILE="${EXPORT_FILE:-}"
IMPORT_FORMAT="${IMPORT_FORMAT:-auto}"
BASE_URL="${BASE_URL:-https://127.0.0.1:8443}"
LOCAL_HOST="${LOCAL_HOST:-127.0.0.1}"
CONTAINER_ENGINE="${CONTAINER_ENGINE:-}"
COMPOSE_RUNNER="${COMPOSE_RUNNER:-}"
FORCE_LOCAL="${FORCE_LOCAL:-0}"
BOOTSTRAP_MODE=0
BOOTSTRAP_WAIT_SECONDS="${BOOTSTRAP_WAIT_SECONDS:-600}"
BOOTSTRAP_CHECK_INTERVAL="${BOOTSTRAP_CHECK_INTERVAL:-5}"

TLS_DIR="${TLS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bitwarden-serve/tls}"
TLS_CERT_FILE="${TLS_CERT_FILE:-$TLS_DIR/cert.pem}"
TLS_KEY_FILE="${TLS_KEY_FILE:-$TLS_DIR/key.pem}"
ALLOW_INSECURE_LOCAL_TLS="${ALLOW_INSECURE_LOCAL_TLS:-1}"
AUTO_LOGIN_LOCAL="${AUTO_LOGIN_LOCAL:-1}"
LOCAL_BW_EMAIL="admin@localhost"
LOCAL_BW_PASSWORD=""
IMPORT_PASSWORD="${IMPORT_PASSWORD:-}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

bw_cmd() {
  if [[ "$ALLOW_INSECURE_LOCAL_TLS" == "1" ]]; then
    NODE_TLS_REJECT_UNAUTHORIZED=0 bw "$@"
  else
    bw "$@"
  fi
}

try_noninteractive_login() {
  if [[ -n "$LOCAL_BW_PASSWORD" ]]; then
    export VW_FALLBACK_BW_PASSWORD="$LOCAL_BW_PASSWORD"
    bw_cmd login "$LOCAL_BW_EMAIL" --passwordenv VW_FALLBACK_BW_PASSWORD --nointeraction
    local rc=$?
    unset VW_FALLBACK_BW_PASSWORD
    return $rc
  fi

  return 1
}

generate_local_password() {
  LOCAL_BW_PASSWORD="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-24)"
  [[ -n "$LOCAL_BW_PASSWORD" ]] || {
    echo "Failed to generate local bootstrap password." >&2
    exit 1
  }
}

detect_container_runtime() {
  if [[ -n "$COMPOSE_RUNNER" ]]; then
    return 0
  fi

  if [[ -n "$CONTAINER_ENGINE" ]]; then
    case "$CONTAINER_ENGINE" in
      docker)
        COMPOSE_RUNNER="docker compose"
        ;;
      podman)
        if podman compose version >/dev/null 2>&1; then
          COMPOSE_RUNNER="podman compose"
        elif command -v podman-compose >/dev/null 2>&1; then
          COMPOSE_RUNNER="podman-compose"
        else
          echo "CONTAINER_ENGINE=podman set, but neither 'podman compose' nor 'podman-compose' is available." >&2
          exit 1
        fi
        ;;
      *)
        echo "Unsupported CONTAINER_ENGINE='$CONTAINER_ENGINE' (expected docker|podman)." >&2
        exit 1
        ;;
    esac
    return 0
  fi

  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    COMPOSE_RUNNER="podman compose"
    return 0
  fi

  if command -v podman-compose >/dev/null 2>&1; then
    COMPOSE_RUNNER="podman-compose"
    return 0
  fi

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_RUNNER="docker compose"
    return 0
  fi

  echo "No compose runtime found (tried podman compose, podman-compose, docker compose)." >&2
  exit 1
}

compose_cmd() {
  # shellcheck disable=SC2086
  $COMPOSE_RUNNER -f "$COMPOSE_FILE" "$@"
}

ensure_tls_material() {
  [[ -f "$TLS_CERT_FILE" && -f "$TLS_KEY_FILE" ]] && return 0
  mkdir -p "$TLS_DIR"
  chmod 700 "$TLS_DIR" 2>/dev/null || true

  openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
    -days 3650 \
    -subj "/CN=127.0.0.1" \
    -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" \
    -keyout "$TLS_KEY_FILE" \
    -out "$TLS_CERT_FILE" >/dev/null 2>&1

  chmod 600 "$TLS_KEY_FILE" "$TLS_CERT_FILE" 2>/dev/null || true
}

set_config_value() {
  local file="$1" key="$2" value="$3"
  local dir
  dir="$(dirname "$file")"
  mkdir -p "$dir"
  touch "$file"
  chmod 600 "$file" 2>/dev/null || true

  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

remove_config_key() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -i "/^${key}=/d" "$file"
}

get_config_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  awk -F= -v k="$key" '$1==k {print substr($0, index($0,$2)); exit}' "$file"
}

resolve_import_format() {
  local import_file="$1"
  local selected="${IMPORT_FORMAT,,}"

  case "$selected" in
    auto)
      if grep -q '"passwordProtected"[[:space:]]*:[[:space:]]*true' "$import_file" 2>/dev/null; then
        printf '%s\n' "bitwardenpasswordprotected"
      else
        printf '%s\n' "bitwardenjson"
      fi
      ;;
    encrypted_json|encryptedjson|bitwardenpasswordprotected)
      printf '%s\n' "bitwardenpasswordprotected"
      ;;
    json|bitwardenjson)
      printf '%s\n' "bitwardenjson"
      ;;
    *)
      printf '%s\n' "$IMPORT_FORMAT"
      ;;
  esac
}

remote_is_reachable() {
  [[ -n "$HOST" ]] || return 1
  getent hosts "$HOST" >/dev/null 2>&1 &&
  curl -fsS --connect-timeout 5 "https://$HOST/api/alive" >/dev/null 2>&1
}

wait_http() {
  local tries=60
  local i
  local curl_opts=(-fsS)
  if [[ "$ALLOW_INSECURE_LOCAL_TLS" == "1" ]]; then
    curl_opts+=(-k)
  fi

  for ((i=1; i<=tries; i++)); do
    if curl "${curl_opts[@]}" "$BASE_URL/alive" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

latest_backup() {
  local latest
  latest="$(ls -1t "$BACKUP_DIR"/vault-*.json 2>/dev/null | head -n1 || true)"
  [[ -n "$latest" ]] || return 1
  printf '%s\n' "$latest"
}

bw_set_server() {
  local target_url="$1"
  local out

  if out="$(bw_cmd config server "$target_url" 2>&1)"; then
    return 0
  fi

  if grep -q "Logout required before server config update" <<<"$out"; then
    echo "bw requires logout before changing server. Logging out and retrying..."
    bw_cmd logout >/dev/null 2>&1 || true
    if out="$(bw_cmd config server "$target_url" 2>&1)"; then
      return 0
    fi
  fi

  printf '%s\n' "$out" >&2
  return 1
}

save_state() {
  # Keep the first pre-fallback snapshot so repeated runs can still restore
  # the original remote configuration on --down.
  if [[ -f "$STATE_FILE" ]]; then
    return 0
  fi

  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR" 2>/dev/null || true

  local prev_host="" prev_url="" prev_bw_server=""
  prev_host="$(get_config_value "$ENV_FILE" "BW_SERVER_HOST" || true)"
  prev_url="$(get_config_value "$ENV_FILE" "BW_SERVER_URL" || true)"
  prev_bw_server="$(bw_cmd config server 2>/dev/null || true)"

  (
    umask 077
    cat > "$STATE_FILE" <<STATE
PREV_BW_SERVER_HOST=${prev_host}
PREV_BW_SERVER_URL=${prev_url}
PREV_BW_CONFIG_SERVER=${prev_bw_server}
STATE
  )
}

load_state() {
  [[ -f "$STATE_FILE" ]] || return 1
  # shellcheck source=/dev/null
  source "$STATE_FILE"
}

switch_to_local() {
  save_state

  set_config_value "$ENV_FILE" "BW_SERVER_HOST" "$LOCAL_HOST"
  set_config_value "$ENV_FILE" "BW_SERVER_URL" "$BASE_URL"

  bw_set_server "$BASE_URL"

  if ! systemctl --user restart bitwarden-cli.service >/dev/null 2>&1; then
    echo "[warn] Could not restart bitwarden-cli.service; continuing bootstrap/fallback flow." >&2
  fi
}

restore_from_state() {
  if ! load_state; then
    echo "No fallback state file found at $STATE_FILE; skipping config restore."
    return 0
  fi

  if [[ -n "${PREV_BW_SERVER_HOST:-}" ]]; then
    set_config_value "$ENV_FILE" "BW_SERVER_HOST" "$PREV_BW_SERVER_HOST"
  else
    remove_config_key "$ENV_FILE" "BW_SERVER_HOST"
  fi

  if [[ -n "${PREV_BW_SERVER_URL:-}" ]]; then
    set_config_value "$ENV_FILE" "BW_SERVER_URL" "$PREV_BW_SERVER_URL"
  else
    remove_config_key "$ENV_FILE" "BW_SERVER_URL"
  fi

  if [[ -n "${PREV_BW_CONFIG_SERVER:-}" ]]; then
    bw_set_server "$PREV_BW_CONFIG_SERVER" >/dev/null || true
  fi

  systemctl --user restart bitwarden-cli.service >/dev/null || true
  rm -f "$STATE_FILE"
}

import_backup() {
  local import_file="$1"
  local status_json session import_format import_password

  status_json="$(bw_cmd status 2>/dev/null || true)"
  if grep -q '"status":"unauthenticated"' <<<"$status_json"; then
    if [[ "$AUTO_LOGIN_LOCAL" == "1" ]]; then
      if [[ -n "$LOCAL_BW_PASSWORD" ]]; then
        echo "No local bw login found. Running non-interactive login for $LOCAL_BW_EMAIL..."
        try_noninteractive_login
      else
        echo "No local bw login found. Running interactive 'bw login' for $BASE_URL..."
        bw_cmd login
      fi
    else
      echo "No local bw login found. Running interactive 'bw login' for $BASE_URL..."
      bw_cmd login
    fi
  fi

  session="$(bw_cmd unlock --raw)"
  export BW_SESSION="$session"

  import_format="$(resolve_import_format "$import_file")"
  import_password="$IMPORT_PASSWORD"
  if [[ -z "$import_password" ]]; then
    import_password="$(get_config_value "$ENV_FILE" "BW_EXPORT_PASSWORD" || true)"
  fi

  echo "Importing backup: $import_file"
  echo "Using importer format: $import_format"
  if [[ "$import_format" == "bitwardenpasswordprotected" && -n "$import_password" ]]; then
    printf '%s\n' "$import_password" | bw_cmd import "$import_format" "$import_file"
  else
    bw_cmd import "$import_format" "$import_file"
  fi
}

bootstrap_wait_for_account() {
  if [[ -z "$LOCAL_BW_PASSWORD" ]]; then
    echo "Bootstrap local password was not generated." >&2
    exit 1
  fi

  local register_url="${BASE_URL%/}/#/register"
  local deadline=$(( $(date +%s) + BOOTSTRAP_WAIT_SECONDS ))
  local now out

  cat <<EOF
Bootstrap mode enabled.
Create the temporary account in your browser now:
  $register_url

Expected account:
  email: $LOCAL_BW_EMAIL
  password: $LOCAL_BW_PASSWORD

The script will wait up to ${BOOTSTRAP_WAIT_SECONDS}s and continue automatically.
EOF

  while true; do
    if out="$(try_noninteractive_login 2>&1)"; then
      echo "Bootstrap account login succeeded."
      return 0
    fi

    now="$(date +%s)"
    if (( now >= deadline )); then
      echo "Timed out waiting for bootstrap account creation/login." >&2
      printf '%s\n' "$out" >&2
      return 1
    fi
    sleep "$BOOTSTRAP_CHECK_INTERVAL"
  done
}

down() {
  compose_cmd down -v
  restore_from_state
}

up_local() {
  [[ -f "$COMPOSE_FILE" ]] || {
    echo "Compose file not found: $COMPOSE_FILE" >&2
    exit 1
  }

  local import_file="$EXPORT_FILE"
  if [[ -z "$import_file" ]]; then
    import_file="$(latest_backup || true)"
  fi

  [[ -n "$import_file" && -f "$import_file" ]] || {
    echo "No backup file found. Set EXPORT_FILE or ensure backups exist in $BACKUP_DIR" >&2
    exit 1
  }

  ensure_tls_material
  export VW_FALLBACK_TLS_DIR="$TLS_DIR"
  compose_cmd up -d
  wait_http || {
    echo "Local Vaultwarden did not become ready at $BASE_URL" >&2
    exit 1
  }

  switch_to_local
  if [[ "$BOOTSTRAP_MODE" == "1" ]]; then
    generate_local_password
    bootstrap_wait_for_account
  fi
  import_backup "$import_file"

  echo "Local fallback is active at $BASE_URL"
  echo "pws now targets local Vaultwarden via updated config."
}

usage() {
  cat <<EOF
Usage:
  $0            Start local Vaultwarden only if remote server is unreachable,
                import latest backup, and switch config to local
  $0 --bootstrap  Start local Vaultwarden and wait for one-time signup/login,
                then import backup and switch config to local
  $0 --down     Stop local fallback and restore previous config

Environment overrides:
  HOST=<your-vaultwarden-host>
  COMPOSE_FILE=$SCRIPT_DIR/docker-compose.yml
  CONTAINER_ENGINE=<docker|podman>   # optional preference
  COMPOSE_RUNNER=<command>           # e.g. "podman compose" or "docker compose"
  BACKUP_DIR=$HOME/.local/share/bitwarden-backup
  EXPORT_FILE=<explicit backup path>
  IMPORT_FORMAT=auto
  IMPORT_PASSWORD=<optional-export-password-override>
  BASE_URL=https://127.0.0.1:8443
  ALLOW_INSECURE_LOCAL_TLS=1
  TLS_DIR=${XDG_CACHE_HOME:-$HOME/.cache}/bitwarden-serve/tls
  AUTO_LOGIN_LOCAL=1
  # bootstrap account is fixed to admin@localhost
  # password is auto-generated and printed by --bootstrap
  BOOTSTRAP_WAIT_SECONDS=600
  BOOTSTRAP_CHECK_INTERVAL=5
  FORCE_LOCAL=0
  ENV_FILE=$XDG_CONFIG/systemd/user/bitwarden-cli.env
EOF
}

main() {
  need_cmd curl
  need_cmd getent
  need_cmd bw
  need_cmd systemctl
  need_cmd openssl
  detect_container_runtime

  case "${1:-}" in
    --down)
      down
      ;;
    --bootstrap)
      BOOTSTRAP_MODE=1
      FORCE_LOCAL=1
      up_local
      ;;
    -h|--help)
      usage
      ;;
    "")
      if [[ -z "$HOST" && "$FORCE_LOCAL" != "1" ]]; then
        echo "HOST is not set. Set HOST to the remote Vaultwarden hostname, or use --down, --bootstrap, or FORCE_LOCAL=1." >&2
        exit 1
      fi
      if [[ "$FORCE_LOCAL" != "1" ]] && remote_is_reachable; then
        echo "Remote Vaultwarden is reachable ($HOST). Not starting local fallback."
        exit 0
      fi
      up_local
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
