#!/bin/sh
# Run a command in the deckery distrobox, inside the test desktop session
# (Atspi/GTK4 layer-shell bindings live in the box). Usable from the host or
# from any other container that has distrobox-host-exec.
#   tools/testdesk/run_box.sh python3 -m deckery_atspi
# RUN_BOX_PIDFILE=<file> records the command's PID (distrobox shares the
# host's PID namespace, and exec keeps it) so it can be stopped without
# pkill patterns, which would also hit the other instance.
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE=/run/user/1000/deckery-testdesk${DECKERY_TESTDESK:-}
CMD="cd '$REPO' && . $STATE/env && unset AT_SPI_BUS_ADDRESS SESSION_MANAGER XAUTHORITY && { [ -z '${RUN_BOX_PIDFILE:-}' ] || echo \$\$ > '${RUN_BOX_PIDFILE:-}'; } && exec \"\$@\""
if command -v distrobox >/dev/null 2>&1; then
    exec distrobox enter deckery -- sh -c "$CMD" sh "$@"
fi
exec distrobox-host-exec distrobox enter deckery -- sh -c "$CMD" sh "$@"
