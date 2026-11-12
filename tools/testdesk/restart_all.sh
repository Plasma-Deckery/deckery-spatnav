#!/bin/sh
# Convenience for the test loop (run from host or a container with
# distrobox-host-exec): fresh test desktop + v2 backend + viewer.
#   [TESTDESK_OUTPUTS=2] [DECKERY_TESTDESK=2] tools/testdesk/restart_all.sh [APP ...]
REPO=$(cd "$(dirname "$0")/../.." && pwd)
INSTANCE="${DECKERY_TESTDESK:-}"
STATE=/run/user/1000/deckery-testdesk$INSTANCE
# distrobox-host-exec does not forward the environment, so pass the instance on.
H="env DECKERY_TESTDESK=$INSTANCE"
command -v kwin_wayland >/dev/null 2>&1 || H="distrobox-host-exec $H"
$H sh -c "$REPO/tools/testdesk/stop.sh; sleep 1; $REPO/tools/testdesk/start.sh ${TESTDESK_OUTPUTS:-1}" >/dev/null 2>&1
[ -s $STATE/env ] || { echo "test desktop failed, see $STATE/kwin.log"; exit 1; }
for app in "$@"; do
    $H sh -c "$REPO/tools/testdesk/run.sh setsid $app > $STATE/app-$(basename "${app%% *}").log 2>&1 &"
done
sleep 4
"$REPO/tools/testdesk/restart_backend.sh"
sleep 1
echo "up"
