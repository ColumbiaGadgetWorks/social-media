#!/bin/sh
# Runs as root only long enough to make the volume folders writable, then drops to
# PUID:PGID (Unraid's nobody:users, 99:100, by default). Docker creates missing
# bind-mount folders as root, which the app user couldn't write to otherwise.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
if [ "$(id -u)" = "0" ]; then
    for dir in /data /media; do
        mkdir -p "$dir"
        chown "$PUID:$PGID" "$dir" 2>/dev/null || true
    done
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
