# E2E image: a real systemd (as PID 1) inside Docker, used to install and
# exercise bitwarden-cli.socket + bitwarden-cli.service end to end.
#
# Usage (from repo root):
#   docker build -t vwcli-e2e -f tests/e2e/Dockerfile tests/e2e
#   docker run -d --name vwcli-e2e --privileged --cgroupns=host \
#     -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
#     --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
#     -v "$PWD:/src:ro" \
#     vwcli-e2e
#   docker exec vwcli-e2e bash /src/tests/e2e/run-e2e.sh

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV container=docker

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       systemd \
       systemd-sysv \
       dbus \
       dbus-user-session \
       libpam-systemd \
       curl \
       ca-certificates \
       iproute2 \
       xz-utils \
       procps \
    && rm -rf /var/lib/apt/lists/*

STOPSIGNAL SIGRTMIN+3

CMD ["/sbin/init"]
