#!/bin/sh
# Stops the test desktop: kwin, its session bus and every app started in it.
# Backend/viewer in the distrobox exit on their own once the session bus /
# Wayland display goes away (Gio's session connection has exit-on-close).
INSTANCE="${DECKERY_TESTDESK:-}"
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk${INSTANCE}"
SOCKET="wayland-deckery-test${INSTANCE}"
if [ -f "$STATE/pid" ]; then
    # setsid made dbus-run-session the leader of its own process group.
    kill -TERM -- "-$(cat "$STATE/pid")" 2>/dev/null
fi
# The nested kwin was seen surviving its session (and pkill -f not matching
# it), so find it by command line and kill it explicitly.
PAT="kwin_wayland --virtual --xwayland --socket $SOCKET "
for p in $(pgrep -f "$PAT"); do kill -TERM "$p"; done 2>/dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$PAT" >/dev/null || break; sleep 0.2; done
for p in $(pgrep -f "$PAT"); do kill -KILL "$p"; done 2>/dev/null
rm -f "$STATE/pid" "$STATE/env" "${XDG_RUNTIME_DIR}/$SOCKET" "${XDG_RUNTIME_DIR}/$SOCKET.lock"
echo "stopped"
