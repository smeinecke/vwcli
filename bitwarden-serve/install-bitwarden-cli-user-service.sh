#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
USER_SYSTEMD_DIR="$XDG_CONFIG/systemd/user"
LOCAL_BIN_DIR="$HOME/.local/bin"
LOCAL_LIB_DIR="$HOME/.local/lib"
ENV_FILE="$USER_SYSTEMD_DIR/bitwarden-cli.env"
REPO_VAULT_PASS_FILE="$SCRIPT_DIR/../../.vault_pass.txt"
VWCLI_CONFIG_DIR="$XDG_CONFIG/vwcli"
VWCLI_CONFIG_FILE="$VWCLI_CONFIG_DIR/config"

NVM_VERSION="v0.40.3"
BW_SERVE_SOCKET="/run/user/$(id -u)/bw.sock"
BW_SESSION_TTL=2505600  # 29 days (conservative vs 30-day Vaultwarden refresh token)
BW_SERVER_URL="${BW_SERVER_URL:-}"

INSTALL_BACKUP=0
INSTALL_VPN_HOOK=0

usage() {
  cat <<EOF
Usage:
  $0 [--install-backup] [--install-vpn-hook]

Options:
  --install-backup   Install and enable bitwarden-export.service/timer
  --install-vpn-hook Install NetworkManager dispatcher hook (requires sudo)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-backup)
        INSTALL_BACKUP=1
        ;;
      --install-vpn-hook)
        INSTALL_VPN_HOOK=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
    shift
  done
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
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
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

  echo "Bitwarden CLI (bw) is not installed."
  confirm "Install Bitwarden CLI now?" || { echo "Cannot continue without Bitwarden CLI." >&2; exit 1; }

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
  (
    umask 077
    printf 'BW_SESSION=%q\nBW_SESSION_EXPIRES=%s\n' "$bw_session" "$expires" > "$ENV_FILE"
  )
  echo "Created $ENV_FILE (token expires in ~29 days)"
}

ensure_export_password() {
  grep -q '^BW_EXPORT_PASSWORD=' "$ENV_FILE" 2>/dev/null && return 0

  local export_password=""
  if [[ -f "$REPO_VAULT_PASS_FILE" ]]; then
    export_password="$(head -n1 "$REPO_VAULT_PASS_FILE")"
  else
    read -rsp "Enter encryption passphrase (BW_EXPORT_PASSWORD): " export_password
    echo
  fi

  [[ -n "$export_password" ]] || { echo "BW_EXPORT_PASSWORD cannot be empty." >&2; exit 1; }

  (umask 077; printf 'BW_EXPORT_PASSWORD=%q\n' "$export_password" >> "$ENV_FILE")
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
check_sources() {
  local src
  for src in \
    "$SCRIPT_DIR/bitwarden-cli.service" \
    "$SCRIPT_DIR/bitwarden-cli.socket" \
    "$SCRIPT_DIR/start-bw-serve.sh" \
    "$SCRIPT_DIR/bw-unix-socket-patch.js" \
    "$SCRIPT_DIR/nm-dispatcher-bitwarden-vpn-up.sh"
  do
    [[ -f "$src" ]] || { echo "Missing required file: $src" >&2; exit 1; }
  done

  if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
    for src in \
      "$SCRIPT_DIR/bitwarden-export.service" \
      "$SCRIPT_DIR/bitwarden-export.timer" \
      "$SCRIPT_DIR/bitwarden-backup.sh"
    do
      [[ -f "$src" ]] || { echo "Missing required file: $src" >&2; exit 1; }
    done
  fi
}

stop_units() {
  if systemctl --user is-active --quiet bitwarden-cli.service; then
    echo "bitwarden-cli.service is running; stopping before update."
    systemctl --user stop bitwarden-cli.service
  fi

  if systemctl --user is-active --quiet bitwarden-cli.socket; then
    echo "bitwarden-cli.socket is active; stopping before update."
    systemctl --user stop bitwarden-cli.socket
  fi

  if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
    if systemctl --user is-active --quiet bitwarden-export.timer; then
      echo "bitwarden-export.timer is running; stopping before update."
      systemctl --user stop bitwarden-export.timer
    fi
  fi
}

install_vpn_hook() {
  if [[ "$INSTALL_VPN_HOOK" -ne 1 ]]; then
    return 0
  fi

  local hook_src="$SCRIPT_DIR/nm-dispatcher-bitwarden-vpn-up.sh"
  local hook_dst="/etc/NetworkManager/dispatcher.d/99-bitwarden-vpn-up"

  if [[ -f "$hook_dst" ]]; then
    echo "VPN dispatcher hook already installed: $hook_dst"
    return 0
  fi

  if confirm "Install NetworkManager VPN dispatcher hook (requires sudo)?"; then
    sudo install -m 0755 -o root -g root "$hook_src" "$hook_dst" \
      && echo "Installed $hook_dst" \
      || { echo "Failed to install VPN dispatcher hook." >&2; }
  fi
}

install_files() {
  mkdir -p "$USER_SYSTEMD_DIR" "$LOCAL_BIN_DIR" "$LOCAL_LIB_DIR"
  install -m 0644 "$SCRIPT_DIR/bitwarden-cli.service"      "$USER_SYSTEMD_DIR/bitwarden-cli.service"
  install -m 0644 "$SCRIPT_DIR/bitwarden-cli.socket"       "$USER_SYSTEMD_DIR/bitwarden-cli.socket"
  install -m 0755 "$SCRIPT_DIR/start-bw-serve.sh"          "$LOCAL_BIN_DIR/start-bw-serve.sh"
  install -m 0644 "$SCRIPT_DIR/bw-unix-socket-patch.js"    "$LOCAL_LIB_DIR/bw-unix-socket-patch.js"

  if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
    install -m 0644 "$SCRIPT_DIR/bitwarden-export.service" "$USER_SYSTEMD_DIR/bitwarden-export.service"
    install -m 0644 "$SCRIPT_DIR/bitwarden-export.timer"   "$USER_SYSTEMD_DIR/bitwarden-export.timer"
    install -m 0755 "$SCRIPT_DIR/bitwarden-backup.sh"      "$LOCAL_BIN_DIR/bitwarden-backup.sh"
  fi
}

setup_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    echo "Keeping existing env file: $ENV_FILE"
  else
    create_env_file
  fi
  if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
    ensure_export_password
  fi
  ensure_bw_server_url
  ensure_bw_session_from_vwcli_config
}

setup_vwcli_config() {
  set_config_value "$VWCLI_CONFIG_FILE" "BW_SERVE_URL" "unix://${BW_SERVE_SOCKET}"
  echo "BW_SERVE_URL set in: $VWCLI_CONFIG_FILE"
}

start_units() {
  systemctl --user daemon-reload
  systemctl --user enable bitwarden-cli.socket bitwarden-cli.service >/dev/null
  systemctl --user reset-failed bitwarden-cli.socket bitwarden-cli.service >/dev/null 2>&1 || true
  systemctl --user start bitwarden-cli.socket
  systemctl --user start bitwarden-cli.service
  if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
    systemctl --user enable bitwarden-export.timer >/dev/null
    systemctl --user start bitwarden-export.timer
    echo "bitwarden-cli.service and bitwarden-export.timer enabled and started for $(id -un)"
    echo "Check status with: systemctl --user status bitwarden-cli.service bitwarden-export.timer"
  else
    echo "bitwarden-cli.service enabled and started for $(id -un)"
    echo "Backup service not installed (use --install-backup to enable it)."
    echo "Check status with: systemctl --user status bitwarden-cli.service"
  fi
}

parse_args "$@"
check_sources
stop_units
install_files
setup_env_file
setup_vwcli_config
install_vpn_hook
start_units
