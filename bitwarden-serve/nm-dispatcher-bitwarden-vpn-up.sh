#!/usr/bin/env bash
# NetworkManager dispatcher: start bitwarden-cli.service when a VPN comes up.
# Install to /etc/NetworkManager/dispatcher.d/ (must be owned by root, mode 0755).
set -euo pipefail

iface="$1"
action="$2"

case "$action" in
  vpn-up) ;;
  up)
    # WireGuard and some other VPN types dispatch 'up' instead of 'vpn-up'.
    con_type="${CONNECTION_TYPE:-}"
    if [[ "$con_type" != *"vpn"* && "$con_type" != *"wireguard"* ]]; then
      exit 0
    fi
    ;;
  *) exit 0 ;;
esac

for runtime_dir in /run/user/*; do
  [[ -d "$runtime_dir" ]] || continue
  uid="$(basename "$runtime_dir")"
  [[ "$uid" =~ ^[0-9]+$ ]] || continue

  bus="$runtime_dir/bus"
  [[ -S "$bus" ]] || continue

  user="$(id -nu "$uid" 2>/dev/null)" || continue

  # Only act if the user actually has the bitwarden-cli service installed.
  if sudo -u "$user" \
       DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
       XDG_RUNTIME_DIR="$runtime_dir" \
       systemctl --user cat bitwarden-cli.service >/dev/null 2>&1; then
    sudo -u "$user" \
       DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
       XDG_RUNTIME_DIR="$runtime_dir" \
       systemctl --user start bitwarden-cli.service >/dev/null 2>&1 || true
  fi
done
