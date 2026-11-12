#!/bin/sh
# (Re)starts only the v2 backend + viewer on this instance; the desktop and
# its apps keep running. Stops the previous ones by the PID files run_box.sh
# writes — never by pkill pattern: the containers share the host's PID
# namespace, so a pattern also hits the other instance (and the calling
# shell, whose command line contains it).
#   [DECKERY_TESTDESK=2] [DECKERY_READER=sync|compare] tools/testdesk/restart_backend.sh
REPO=$(cd "$(dirname "$0")/../.." && pwd)
INSTANCE="${DECKERY_TESTDESK:-}"
STATE=/run/user/1000/deckery-testdesk$INSTANCE
for name in backend viewer; do
    [ -s "$STATE/$name.pid" ] && kill "$(cat "$STATE/$name.pid")" 2>/dev/null
    rm -f "$STATE/$name.pid"
done
sleep 1
setsid env DECKERY_TESTDESK="$INSTANCE" RUN_BOX_PIDFILE="$STATE/backend.pid" "$REPO/tools/testdesk/run_box.sh" \
    env DECKERY_READER="${DECKERY_READER:-async}" DECKERY_DEBUG_MENUS=1 DECKERY_DEBUG_EVENTS=1 python3 -u -W ignore -m deckery_atspi > "$STATE/backend.log" 2>&1 &
sleep 4
setsid env DECKERY_TESTDESK="$INSTANCE" RUN_BOX_PIDFILE="$STATE/viewer.pid" "$REPO/tools/testdesk/run_box.sh" \
    python3 -u -W ignore tools/v2_viewer.py > "$STATE/viewer.log" 2>&1 &
sleep 4
