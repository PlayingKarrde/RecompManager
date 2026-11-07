#!/bin/sh
# Start as root only long enough to fix ownership, then drop to PUID:PGID.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
SOCK=/var/run/docker.sock

umask "$UMASK"

if [ "$(id -u)" != "0" ]; then
  # Already running as a non-root user (e.g. `docker run --user`): nothing to fix.
  exec "$@"
fi

mkdir -p /config /data

# /config is small (appdata): fix ownership recursively, but only what's wrong.
find /config -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) \
  -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

# /data can hold terabytes: never chown it recursively. Make sure our top-level
# folders exist and belong to us; their contents are created by us anyway.
for d in /data/library /data/inputs /data/builds; do
  [ -d "$d" ] || mkdir -p "$d"
  chown "$PUID:$PGID" "$d" 2>/dev/null || true
done

# Server-side builds talk to the host's Docker daemon through its socket. Give
# the app the socket's group so it can use it. Mounting the socket is
# root-equivalent on the host; it's optional and off unless mounted.
GROUPS_OPT="--clear-groups"
if [ -S "$SOCK" ]; then
  GROUPS_OPT="--groups=$(stat -c %g "$SOCK")"
fi

exec setpriv --reuid="$PUID" --regid="$PGID" "$GROUPS_OPT" -- "$@"
