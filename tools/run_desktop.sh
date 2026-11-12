#!/bin/sh
# Run the v2 backend (or the viewer) on the real desktop, in the deckery
# distrobox. Usable from the host or from any container with
# distrobox-host-exec.
#   tools/run_desktop.sh backend     # python3 -m deckery_atspi
#   tools/run_desktop.sh viewer      # tools/v2_viewer.py
# Extra env (DECKERY_READER, DECKERY_DEBUG_*) is passed through.
REPO=$(cd "$(dirname "$0")/.." && pwd)
case "$1" in
    backend) PROG="python3 -m deckery_atspi" ;;
    viewer)  PROG="python3 tools/v2_viewer.py" ;;
    *) echo "usage: $0 backend|viewer" >&2; exit 2 ;;
esac
ENVS="DECKERY_READER='${DECKERY_READER:-async}' DECKERY_DEBUG_MENUS='${DECKERY_DEBUG_MENUS:-}' DECKERY_DEBUG_EVENTS='${DECKERY_DEBUG_EVENTS:-}' DECKERY_DEBUG_POLL='${DECKERY_DEBUG_POLL:-}'"
# A stale AT_SPI_BUS_ADDRESS from the container env points at a dead socket;
# without it libatspi asks org.a11y.Bus for the live one.
CMD="cd '$REPO' && unset AT_SPI_BUS_ADDRESS && exec env PYTHONUNBUFFERED=1 $ENVS $PROG"
if command -v distrobox >/dev/null 2>&1; then
    exec distrobox enter deckery -- sh -c "$CMD"
fi
exec distrobox-host-exec distrobox enter deckery -- sh -c "$CMD"
