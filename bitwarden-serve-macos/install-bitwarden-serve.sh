#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
VWCLI_CONFIG_DIR="$XDG_CONFIG/vwcli"
VWCLI_CONFIG_FILE="$VWCLI_CONFIG_DIR/config"
ENV_FILE="$VWCLI_CONFIG_DIR/bw-serve.env"
LOCAL_BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$LAUNCH_AGENTS_DIR/com.vwcli.bitwarden-serve.plist"
LAUNCHD_LABEL="com.vwcli.bitwarden-serve"
LOG_DIR="$HOME/Library/Logs/vwcli"
SOCKET_DIR="$HOME/Library/Application Support/vwcli"
BW_SERVE_SOCKET="$SOCKET_DIR/bw.sock"

NVM_VERSION="v0.40.3"
BW_SESSION_TTL=2505600 # 29 days (conservative vs 30-day Vaultwarden refresh token)
BW_SERVER_URL="${BW_SERVER_URL:-}"

usage() {
  cat <<EOF
Usage:
  $0

Installs a launchd LaunchAgent running 'bw serve' on a unix socket.
Must be run on macOS.
EOF
}

confirm() {
  local prompt="${1:-Continue?}" reply
  read -rp "$prompt [y/N]: " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# Write or replace a KEY=VALUE line in a config file (mode 600, dir mode 700).
set_config_value() {
  local file="$1" dir key="$2" value="$3"
  dir="$(dirname "$file")"
  mkdir -p "$dir" && chmod 700 "$dir"
  touch "$file"   && chmod 600 "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i '' "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

load_nvm() {
  [[ -s "$HOME/.nvm/nvm.sh" ]] || return 1
  # shellcheck source=/dev/null
  source "$HOME/.nvm/nvm.sh"
}

ensure_nvm() {
  load_nvm && return 0

  echo "nvm is not installed."
  confirm "Install nvm now?" || { echo "Cannot continue without nvm." >&2; exit 1; }

  curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh" | bash

  load_nvm || { echo "nvm installed but could not be loaded." >&2; exit 1; }
}

run_bw() {
  if command -v bw >/dev/null 2>&1; then
    bw "$@"
  elif load_nvm; then
    nvm exec --silent --lts -- bw "$@"
  else
    echo "bw not in PATH and nvm not available." >&2
    return 127
  fi
}

ensure_bw_cli() {
  command -v bw >/dev/null 2>&1 && return 0

  if command -v brew >/dev/null 2>&1; then
    echo "Bitwarden CLI (bw) is not installed."
    if confirm "Install it via 'brew install bitwarden-cli'?"; then
      brew install bitwarden-cli
      return 0
    fi
  else
    echo "Bitwarden CLI (bw) is not installed and Homebrew was not found."
  fi

  confirm "Install Bitwarden CLI via nvm instead?" || { echo "Cannot continue without Bitwarden CLI." >&2; exit 1; }
  ensure_nvm
  nvm install --lts >/dev/null
  nvm exec --silent --lts -- npm install -g @bitwarden/cli >/dev/null
  nvm exec --silent --lts -- command -v bw >/dev/null 2>&1 \
    || { echo "Bitwarden CLI installation failed." >&2; exit 1; }
}

create_bw_session_token() {
  local status_json
  status_json="$(run_bw status 2>/dev/null || true)"
  if grep -q '"status":"unauthenticated"' <<<"$status_json"; then
    echo "No Bitwarden login found. Running 'bw login'..." >&2
    run_bw config server "$BW_SERVER_URL"
    run_bw login
  fi
  echo "Unlocking vault to generate session token..." >&2
  run_bw unlock --raw
}

create_env_file() {
  ensure_bw_cli

  if [[ -z "$BW_SERVER_URL" ]]; then
    read -rp "Vaultwarden server URL (e.g. https://vault.example.com): " BW_SERVER_URL
  fi
  [[ -n "$BW_SERVER_URL" ]] || { echo "BW_SERVER_URL cannot be empty." >&2; exit 1; }

  local bw_session
  echo "Creating $ENV_FILE"
  if confirm "Do you already have a Bitwarden BW_SESSION token?"; then
    read -rsp "Enter Bitwarden session token (BW_SESSION): " bw_session
    echo
  else
    bw_session="$(create_bw_session_token)"
  fi

  [[ -n "$bw_session" ]] || { echo "BW_SESSION token cannot be empty." >&2; exit 1; }

  local expires=$(( $(date +%s) + BW_SESSION_TTL ))
  mkdir -p "$VWCLI_CONFIG_DIR" && chmod 700 "$VWCLI_CONFIG_DIR"
  (
    umask 077
    printf 'BW_SESSION=%q\nBW_SESSION_EXPIRES=%s\n' "$bw_session" "$expires" > "$ENV_FILE"
  )
  echo "Created $ENV_FILE (token expires in ~29 days)"
}

ensure_bw_server_url() {
  grep -q '^BW_SERVER_URL=' "$ENV_FILE" 2>/dev/null && return 0
  (umask 077; printf 'BW_SERVER_URL=%q\n' "$BW_SERVER_URL" >> "$ENV_FILE")
}

ensure_bw_session_from_vwcli_config() {
  grep -q '^BW_SESSION=' "$ENV_FILE" 2>/dev/null && return 0
  [[ -f "$VWCLI_CONFIG_FILE" ]] || return 0

  local vwcli_session
  vwcli_session="$(awk '/^BW_SESSION=/{print substr($0, index($0,"=")+1); exit}' "$VWCLI_CONFIG_FILE")"
  [[ -n "$vwcli_session" ]] || return 0

  (umask 077; printf 'BW_SESSION=%q\n' "$vwcli_session" >> "$ENV_FILE")
  echo "Recovered BW_SESSION from $VWCLI_CONFIG_FILE into $ENV_FILE"
}

# Install steps
check_platform() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    echo "This installer is for macOS only. On Linux use bitwarden-serve/install-bitwarden-cli-user-service.sh." >&2
    exit 1
  }
}

check_sources() {
  local src
  for src in \
    "$SCRIPT_DIR/com.vwcli.bitwarden-serve.plist" \
    "$SCRIPT_DIR/start-bw-serve.sh"
  do
    [[ -f "$src" ]] || { echo "Missing required file: $src" >&2; exit 1; }
  done
}

install_start_script() {
  mkdir -p "$LOCAL_BIN_DIR" "$LOG_DIR" "$SOCKET_DIR"
  chmod 700 "$SOCKET_DIR"
  install -m 0755 "$SCRIPT_DIR/start-bw-serve.sh" "$LOCAL_BIN_DIR/start-bw-serve.sh"
  echo "Installed start-bw-serve.sh -> $LOCAL_BIN_DIR/start-bw-serve.sh"
}

install_plist() {
  mkdir -p "$LAUNCH_AGENTS_DIR"
  sed -e "s|__START_SCRIPT__|$LOCAL_BIN_DIR/start-bw-serve.sh|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      "$SCRIPT_DIR/com.vwcli.bitwarden-serve.plist" > "$PLIST_FILE"
  chmod 644 "$PLIST_FILE"
  echo "Installed LaunchAgent -> $PLIST_FILE"
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    create_env_file
  else
    echo "Found existing env file at $ENV_FILE"
  fi
  ensure_bw_server_url
  ensure_bw_session_from_vwcli_config
}

service_loaded() {
  launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1
}

load_service() {
  local uid
  uid="$(id -u)"
  if service_loaded; then
    launchctl kickstart -k "gui/$uid/$LAUNCHD_LABEL" || true
  else
    launchctl bootstrap "gui/$uid" "$PLIST_FILE" || true
  fi
  if service_loaded; then
    echo "Service '$LAUNCHD_LABEL' loaded."
  else
    echo "Warning: service did not load. Check $LOG_DIR/bitwarden-serve.err.log" >&2
  fi
}

configure_vwcli_socket() {
  set_config_value "$VWCLI_CONFIG_FILE" "BW_SERVE_URL" "unix://$BW_SERVE_SOCKET"
  echo "Updated $VWCLI_CONFIG_FILE with BW_SERVE_URL=unix://$BW_SERVE_SOCKET"
}

main() {
  check_platform
  check_sources
  install_start_script
  install_plist
  ensure_env_file
  load_service
  configure_vwcli_socket

  echo
  echo "Done."
  echo "  - Socket: $BW_SERVE_SOCKET"
  echo "  - Logs:   $LOG_DIR/bitwarden-serve{,.err}.log"
  echo "  - Unload: launchctl bootout gui/\$(id -u) $PLIST_FILE"
}

main "$@"
