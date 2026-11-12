#!/bin/sh
# Run a command in the deckery distrobox, inside the test desktop session
# (Atspi/GTK4 layer-shell bindings live in the box). Usable from the host or
# from any other container that has distrobox-host-exec.
#   tools/testdesk/run_box.sh python3 -m deckery_atspi
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE=/run/user/1000/deckery-testdesk${DECKERY_TESTDESK:-}
CMD="cd '$REPO' && . $STATE/env && unset AT_SPI_BUS_ADDRESS SESSION_MANAGER XAUTHORITY && exec \"\$@\""
if command -v distrobox >/dev/null 2>&1; then
    exec distrobox enter deckery -- sh -c "$CMD" sh "$@"
fi
exec distrobox-host-exec distrobox enter deckery -- sh -c "$CMD" sh "$@"
