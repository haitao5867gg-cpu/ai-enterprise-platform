#!/bin/sh
# The media volume belongs to the API (root). The connector writes only its own folder
# under _work, so it gives that folder to its user (the media connector's, 65532, which
# cleans _work too) and drops root.
set -eu
work="${AEP_MEDIA_DIR:-/data/media}/_work"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$work/stock"
  chown 65532:65532 "$work"
  chown -R 65532:65532 "$work/stock"
  exec setpriv --reuid=65532 --regid=65532 --clear-groups -- "$@"
fi
exec "$@"
