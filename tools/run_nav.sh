#!/bin/sh
# Run the navigation prototype on the real desktop, in the deckery distrobox
# (from the host or any container with distrobox-host-exec). Reads the running
# backend, makima's state file, moves the real pointer.
#   tools/run_nav.sh                 # with makima's analog export kept on
#   tools/run_nav.sh --no-pointer    # extra args go to python3 -m deckery_nav
REPO=$(cd "$(dirname "$0")/.." && pwd)
ARGS="--ensure-analog-export"
for a in "$@"; do ARGS="$ARGS '$a'"; done
CMD="cd '$REPO' && unset AT_SPI_BUS_ADDRESS && exec env PYTHONUNBUFFERED=1 python3 -m deckery_nav $ARGS"
if command -v distrobox >/dev/null 2>&1; then
    exec distrobox enter deckery -- sh -c "$CMD"
fi
exec distrobox-host-exec distrobox enter deckery -- sh -c "$CMD"
