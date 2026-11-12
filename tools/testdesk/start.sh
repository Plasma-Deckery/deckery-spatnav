#!/bin/sh
# Starts an invisible, isolated KDE test desktop: nested kwin_wayland on a
# virtual framebuffer with its own session D-Bus (→ own AT-SPI bus, own KWin
# scripting). Nothing here touches the real desktop. Run on the HOST.
#
#   [DECKERY_TESTDESK=2] tools/testdesk/start.sh [OUTPUTS] [WIDTH] [HEIGHT] [SCALE]
#
# WIDTH/HEIGHT are logical. Defaults mimic the Steam Deck panel: 1164x727
# logical = 1280x800 physical at scale 1.1. kwin --virtual uses
# --scale only to size the mode (WIDTH*SCALE) but leaves the output at scale
# 1, so session.sh sets the output scale with kscreen-doctor.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
# DECKERY_TESTDESK=<suffix> runs a second, independent instance (own
# socket, state dir, session bus) next to the default one.
INSTANCE="${DECKERY_TESTDESK:-}"
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk${INSTANCE}"
mkdir -p "$STATE"
if [ -f "$STATE/pid" ] && kill -0 "$(cat "$STATE/pid")" 2>/dev/null; then
    echo "test desktop already running (pid $(cat "$STATE/pid"))"; exit 0
fi
rm -f "$STATE/env"
# Own config/data/cache: tests click around in System Settings etc. and must
# never change the real desktop's settings (theme, kwinoutputconfig.json, …).
# Wiped on every start so each run begins from KDE defaults.
rm -rf "$STATE/home"
mkdir -p "$STATE/home/config" "$STATE/home/data" "$STATE/home/cache" "$STATE/home/state"
export XDG_CONFIG_HOME="$STATE/home/config" XDG_DATA_HOME="$STATE/home/data" \
       XDG_CACHE_HOME="$STATE/home/cache" XDG_STATE_HOME="$STATE/home/state"
OUTPUTS=${1:-1}; WIDTH=${2:-1164}; HEIGHT=${3:-727}; SCALE=${4:-1.1}
export DECKERY_TESTDESK_OUTPUTS="$OUTPUTS" DECKERY_TESTDESK_SCALE="$SCALE"
# Strip everything that would point apps at the REAL session.
env -u AT_SPI_BUS_ADDRESS -u DISPLAY -u WAYLAND_DISPLAY -u SESSION_MANAGER \
    -u DBUS_SESSION_BUS_ADDRESS -u XAUTHORITY -u XDG_SESSION_ID \
    setsid dbus-run-session -- kwin_wayland --virtual --xwayland --socket "wayland-deckery-test${INSTANCE}" \
        --no-lockscreen --no-global-shortcuts --no-kactivities \
        --output-count "$OUTPUTS" --width "$WIDTH" --height "$HEIGHT" --scale "$SCALE" \
        --exit-with-session "$HERE/session.sh" \
        > "$STATE/kwin.log" 2>&1 &
echo $! > "$STATE/pid"
for i in $(seq 1 50); do
    [ -s "$STATE/env" ] && { echo "test desktop up:"; cat "$STATE/env"; exit 0; }
    sleep 0.2
done
echo "test desktop did not come up, see $STATE/kwin.log"; tail -20 "$STATE/kwin.log"; exit 1
