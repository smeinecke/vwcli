#!/usr/bin/env bash
# E2E test for the real systemd socket-activation units.
# Expects a real bw session seed at /seed/ (BW_SESSION + Bitwarden CLI data dir).
# Runs inside a privileged systemd container, installs the user service, and
# verifies bw serve serves HTTP over /run/user/<uid>/bw.sock without a TCP port.
set -euo pipefail

TESTUSER="${TESTUSER:-tester}"
SEED_DIR="${SEED_DIR:-/seed}"

log() { printf '\n==> %s\n' "$*"; }
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  if id -u "$TESTUSER" >/dev/null 2>&1; then
    TEST_UID="$(id -u "$TESTUSER")"
    if systemctl is-active --quiet "user@${TEST_UID}.service" 2>/dev/null; then
      su - "$TESTUSER" -s /bin/bash -c 'journalctl --user --no-pager -u bitwarden-cli.service -u bitwarden-cli.socket -n 100' || true
    fi
  fi
  exit 1
}

log "Creating test user with lingering user manager"
useradd -m -s /bin/bash "$TESTUSER"
loginctl enable-linger "$TESTUSER"
TEST_UID="$(id -u "$TESTUSER")"

log "Waiting for the systemd user manager to start"
for _ in $(seq 1 60); do
  if systemctl is-active --quiet "user@${TEST_UID}.service"; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet "user@${TEST_UID}.service" || fail "user manager did not start"

# Ensure the user's session environment has a runtime dir and bus.
# (libpam-systemd should already set this, but be explicit for safety.)
cat >/etc/profile.d/zz-e2e-env.sh <<'EOS'
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
EOS

log "Installing nvm, Node LTS and @bitwarden/cli as $TESTUSER"
su - "$TESTUSER" -s /bin/bash -c 'bash -s' <<'EOS'
export NVM_DIR="$HOME/.nvm"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
. "$NVM_DIR/nvm.sh"
nvm install --lts
nvm exec --silent --lts -- npm install -g @bitwarden/cli
. "$NVM_DIR/nvm.sh" && nvm exec --silent --lts -- bw --version
EOS

log "Copying Bitwarden CLI data and session into $TESTUSER home"
if [[ ! -d "$SEED_DIR/Bitwarden CLI" ]]; then
  fail "Bitwarden CLI data dir not found in $SEED_DIR"
fi
if [[ ! -f "$SEED_DIR/session.env" ]]; then
  fail "BW_SESSION file not found in $SEED_DIR"
fi

BW_SESSION_VALUE="$(grep '^BW_SESSION=' "$SEED_DIR/session.env" | cut -d= -f2-)"
[[ -n "$BW_SESSION_VALUE" ]] || fail "BW_SESSION is empty"

mkdir -p "/home/$TESTUSER/.config"
cp -r "$SEED_DIR/Bitwarden CLI" "/home/$TESTUSER/.config/"
chown -R "$TESTUSER:$TESTUSER" "/home/$TESTUSER/.config"

log "Pre-seeding env file so the installer is non-interactive"
mkdir -p "/home/$TESTUSER/.config/systemd/user"
printf 'BW_SESSION=%s\n' "$BW_SESSION_VALUE" > "/home/$TESTUSER/.config/systemd/user/bitwarden-cli.env"
chown -R "$TESTUSER:$TESTUSER" "/home/$TESTUSER/.config/systemd/user"
chmod 600 "/home/$TESTUSER/.config/systemd/user/bitwarden-cli.env"

log "Running install-bitwarden-cli-user-service.sh"
su - "$TESTUSER" -s /bin/bash -c 'bash -s' <<'EOS'
cd /src/bitwarden-serve
bash ./install-bitwarden-cli-user-service.sh
EOS

# The GH Actions Docker user manager cannot drop the capability bounding set
# for a regular user (it lacks CAP_SETPCAP in this environment), so remove the
# directives that trigger a cap drop. The SocketBindDeny=any restriction that
# prevents the service from binding TCP is still tested.
log "Relaxing capability directives for container user manager"
sed -i '/^CapabilityBoundingSet=/d' "/home/$TESTUSER/.config/systemd/user/bitwarden-cli.service"
su - "$TESTUSER" -s /bin/bash -c 'systemctl --user daemon-reload && systemctl --user restart bitwarden-cli.socket bitwarden-cli.service'

BW_SOCK="/run/user/${TEST_UID}/bw.sock"

log "Waiting for socket and service"
for _ in $(seq 1 120); do
  [[ -S "$BW_SOCK" ]] && su - "$TESTUSER" -s /bin/bash -c 'systemctl --user is-active --quiet bitwarden-cli.service' && break
  sleep 1
done

[[ -S "$BW_SOCK" ]] || fail "socket $BW_SOCK was not created"
su - "$TESTUSER" -s /bin/bash -c 'systemctl --user is-active --quiet bitwarden-cli.service' || fail "bitwarden-cli.service is not active"

SOCK_MODE="$(stat -c '%a' "$BW_SOCK")"
[[ "$SOCK_MODE" == "600" ]] || fail "socket mode is $SOCK_MODE, expected 600"

BW_PID="$(su - "$TESTUSER" -s /bin/bash -c 'systemctl --user show bitwarden-cli.service -p MainPID --value')"
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
su - "$TESTUSER" -s /bin/bash -c 'systemctl --user restart bitwarden-cli.service'
for _ in $(seq 1 60); do
  su - "$TESTUSER" -s /bin/bash -c 'systemctl --user is-active --quiet bitwarden-cli.service' && break
  sleep 1
done
su - "$TESTUSER" -s /bin/bash -c 'systemctl --user is-active --quiet bitwarden-cli.service' || fail "service did not restart"

# PID may have changed on restart.
BW_PID="$(su - "$TESTUSER" -s /bin/bash -c 'systemctl --user show bitwarden-cli.service -p MainPID --value')"

log "HTTP over the unix socket"
RESPONSE="$(mktemp)"
HTTP_CODE="$(curl -sS -o "$RESPONSE" -w '%{http_code}' --unix-socket "$BW_SOCK" --max-time 10 http://localhost/status)"
cat "$RESPONSE"; rm -f "$RESPONSE"
[[ "$HTTP_CODE" =~ ^[0-9]+$ ]] && [[ "$HTTP_CODE" -ne 000 ]] || fail "no HTTP response over unix socket (code: $HTTP_CODE)"
[[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]] || fail "status endpoint returned HTTP $HTTP_CODE"

# Confirm SocketBindDeny=any is configured on the unit.
su - "$TESTUSER" -s /bin/bash -c 'systemctl --user show bitwarden-cli.service -p SocketBindDeny' | grep -q 'any' || fail "SocketBindDeny=any not in effect"

echo
echo "PASS: socket-activated bw serve serves HTTP over $BW_SOCK"
echo "      (mode $SOCK_MODE, fd 3, no TCP listener, SocketBindDeny=any)"
