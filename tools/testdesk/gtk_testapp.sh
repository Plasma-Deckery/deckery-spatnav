#!/bin/sh
# (Re)starts tools/testdesk/gtk_testapp.py inside the test desktop, in the
# deckery distrobox (GTK4 bindings live there). Stops the previous one by
# its PID file, never by pkill pattern (the containers share the host's
# PID namespace). Run on the HOST, or as an app of restart_all.sh:
#   [DECKERY_TESTDESK=2] tools/testdesk/gtk_testapp.sh
#   tools/testdesk/restart_all.sh tools/testdesk/gtk_testapp.sh dolphin
HERE=$(cd "$(dirname "$0")" && pwd)
STATE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/deckery-testdesk${DECKERY_TESTDESK:-}"
[ -s "$STATE/gtkapp.pid" ] && kill "$(cat "$STATE/gtkapp.pid")" 2>/dev/null && sleep 1
rm -f "$STATE/gtkapp.pid"
# restart_all.sh starts apps through run.sh, which exports the test
# desktop's own XDG dirs — podman would look for the distrobox there.
env -u XDG_CONFIG_HOME -u XDG_DATA_HOME -u XDG_CACHE_HOME -u XDG_STATE_HOME \
    RUN_BOX_PIDFILE="$STATE/gtkapp.pid" setsid "$HERE/run_box.sh" \
    python3 -u tools/testdesk/gtk_testapp.py > "$STATE/app-gtk.log" 2>&1 < /dev/null &
