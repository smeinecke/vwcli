#!/usr/bin/env bash
# E2E test for the real systemd socket-activation units.
# Expects a real bw session seed at /seed/ (BW_SESSION + Bitwarden CLI data dir).
# Runs inside a privileged systemd container as root's user manager (the only
# manager there that has the capabilities the unit's sandbox directives need),
# installs the user service, and verifies bw serve serves HTTP over
# /run/user/0/bw.sock without a TCP port.
set -euo pipefail

SEED_DIR="${SEED_DIR:-/seed}"
export HOME=/root
export XDG_RUNTIME_DIR=/run/user/0
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
TEST_UID=0

log() { printf '\n==> %s\n' "$*"; }
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  journalctl --user --no-pager -u bitwarden-cli.service -u bitwarden-cli.socket -n 100 2>/dev/null || true
  exit 1
}

log "Starting root's systemd user manager"
systemctl start user@0.service
for _ in $(seq 1 60); do
  if systemctl is-active --quiet user@0.service; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet user@0.service || fail "root user manager did not start"

log "Installing nvm, Node LTS and @bitwarden/cli"
export NVM_DIR="$HOME/.nvm"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"
nvm install --lts
nvm exec --silent --lts -- npm install -g @bitwarden/cli
nvm exec --silent --lts -- bw --version

log "Copying Bitwarden CLI data and session into root home"
if [[ ! -d "$SEED_DIR/Bitwarden CLI" ]]; then
  fail "Bitwarden CLI data dir not found in $SEED_DIR"
fi
if [[ ! -f "$SEED_DIR/session.env" ]]; then
  fail "BW_SESSION file not found in $SEED_DIR"
fi

BW_SESSION_VALUE="$(grep '^BW_SESSION=' "$SEED_DIR/session.env" | cut -d= -f2-)"
[[ -n "$BW_SESSION_VALUE" ]] || fail "BW_SESSION is empty"

mkdir -p "$HOME/.config"
cp -r "$SEED_DIR/Bitwarden CLI" "$HOME/.config/"

log "Pre-seeding env file so the installer is non-interactive"
mkdir -p "$HOME/.config/systemd/user"
printf 'BW_SESSION=%s\n' "$BW_SESSION_VALUE" > "$HOME/.config/systemd/user/bitwarden-cli.env"
chmod 600 "$HOME/.config/systemd/user/bitwarden-cli.env"

log "Running install-bitwarden-cli-user-service.sh"
cd /src/bitwarden-serve
bash ./install-bitwarden-cli-user-service.sh

BW_SOCK="/run/user/${TEST_UID}/bw.sock"

log "Waiting for socket and service"
for _ in $(seq 1 120); do
  [[ -S "$BW_SOCK" ]] && systemctl --user is-active --quiet bitwarden-cli.service && break
  sleep 1
done

[[ -S "$BW_SOCK" ]] || fail "socket $BW_SOCK was not created"
systemctl --user is-active --quiet bitwarden-cli.service || fail "bitwarden-cli.service is not active"

SOCK_MODE="$(stat -c '%a' "$BW_SOCK")"
[[ "$SOCK_MODE" == "600" ]] || fail "socket mode is $SOCK_MODE, expected 600"

BW_PID="$(systemctl --user show bitwarden-cli.service -p MainPID --value)"
[[ "$BW_PID" =~ ^[0-9]+$ ]] && [[ "$BW_PID" -gt 0 ]] || fail "no MainPID for service"

log "Verifying fd handoff: LISTEN_FDS, LISTEN_PID, fd 3"
tr '\0' '\n' <"/proc/$BW_PID/environ" | grep -q '^LISTEN_FDS=1$' || fail "LISTEN_FDS missing/incorrect"
tr '\0' '\n' <"/proc/$BW_PID/environ" | grep -q "^LISTEN_PID=$BW_PID$" || fail "LISTEN_PID mismatch"
readlink "/proc/$BW_PID/fd/3" | grep -q '^socket:' || fail "fd 3 is not a socket"

log "Verifying no TCP listener"
if ss -ltnp | grep -q "pid=$BW_PID,"; then
  ss -ltnp | grep "pid=$BW_PID,"
  fail "bw serve (pid $BW_PID) is listening on TCP"
fi

log "Restarting service to confirm socket survives restarts"
systemctl --user restart bitwarden-cli.service
for _ in $(seq 1 60); do
  systemctl --user is-active --quiet bitwarden-cli.service && break
  sleep 1
done
systemctl --user is-active --quiet bitwarden-cli.service || fail "service did not restart"

# PID may have changed on restart.
BW_PID="$(systemctl --user show bitwarden-cli.service -p MainPID --value)"

log "HTTP over the unix socket"
RESPONSE="$(mktemp)"
HTTP_CODE="$(curl -sS -o "$RESPONSE" -w '%{http_code}' --unix-socket "$BW_SOCK" --max-time 10 http://localhost/status)"
cat "$RESPONSE"; rm -f "$RESPONSE"
[[ "$HTTP_CODE" =~ ^[0-9]+$ ]] && [[ "$HTTP_CODE" -ne 000 ]] || fail "no HTTP response over unix socket (code: $HTTP_CODE)"
[[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]] || fail "status endpoint returned HTTP $HTTP_CODE"

# Confirm SocketBindDeny=any is configured on the unit.
systemctl --user show bitwarden-cli.service -p SocketBindDeny | grep -q 'any' || fail "SocketBindDeny=any not in effect"

echo
echo "PASS: socket-activated bw serve serves HTTP over $BW_SOCK"
echo "      (mode $SOCK_MODE, fd 3, no TCP listener, SocketBindDeny=any)"
