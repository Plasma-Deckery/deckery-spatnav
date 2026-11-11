#!/bin/sh
# Convenience for the test loop (run from host or a container with
# distrobox-host-exec): fresh test desktop + v2 backend + viewer.
#   [TESTDESK_OUTPUTS=2] tools/testdesk/restart_all.sh [APP ...]
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE=/run/user/1000/deckery-testdesk
H=""; command -v kwin_wayland >/dev/null 2>&1 || H=distrobox-host-exec
$H sh -c "$REPO/tools/testdesk/stop.sh; sleep 1; $REPO/tools/testdesk/start.sh ${TESTDESK_OUTPUTS:-1}" >/dev/null 2>&1
[ -s $STATE/env ] || { echo "test desktop failed, see $STATE/kwin.log"; exit 1; }
for app in "$@"; do
    $H sh -c "$REPO/tools/testdesk/run.sh setsid $app > $STATE/app-$(basename "${app%% *}").log 2>&1 &"
done
sleep 4
setsid "$REPO/tools/testdesk/run_box.sh" env DECKERY_DEBUG_MENUS=1 python3 -u -W ignore -m deckery_atspi > $STATE/backend.log 2>&1 &
sleep 4
setsid "$REPO/tools/testdesk/run_box.sh" python3 -u -W ignore tools/v2_viewer.py > $STATE/viewer.log 2>&1 &
sleep 5
echo "up"
