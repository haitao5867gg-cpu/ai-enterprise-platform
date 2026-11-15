#!/bin/sh
# The media volume belongs to the API (root). The connector only reads the libraries and
# writes its own work folder, so it gives that folder to its user and drops root.
set -eu
work="${AEP_MEDIA_DIR:-/data/media}/_work"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$work"
  chown -R 65532:65532 "$work"
  exec setpriv --reuid=65532 --regid=65532 --clear-groups -- "$@"
fi
exec "$@"
