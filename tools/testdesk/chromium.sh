#!/bin/sh
# (Re)starts the Chromium Flatpak inside the test desktop with a throwaway
# profile, never the real one: --user-data-dir under the Flatpak's own
# ~/.var/app dir (the only place the sandbox can write), --password-store=basic
# so it never asks KWallet. Run on the HOST. Extra args go to `flatpak run`;
# $CHROMIUM_ARGS to Chromium.
#   [CHROMIUM_PAGE=controls.html] tools/testdesk/chromium.sh [--env=VAR=1 ...]
#
# Chromium 156 Flatpak exposes nothing to AT-SPI on its own (test desktop
# 2026-11-12): the sandbox may not ask org.a11y.Bus whether a11y is on, so
# it never registers — ACCESSIBILITY_ENABLED=1 (or --talk-name=org.a11y.Bus)
# fixes that, but even then the frame stays empty until
# --force-renderer-accessibility, for the browser's own UI too.
HERE=$(cd "$(dirname "$0")" && pwd)
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk${DECKERY_TESTDESK:-}"
DIR="$HOME/.var/app/org.chromium.Chromium/deckery-testdesk${DECKERY_TESTDESK:-}"
mkdir -p "$DIR"
cp "$HERE"/*.html "$DIR/"
# [p] keeps pkill from matching this script's own command line
pkill -f "[d]eckery-testdesk${DECKERY_TESTDESK:-}/profile" && sleep 2
# The test desktop has its own XDG_DATA_HOME; point flatpak back at the
# real user installation.
FLATPAK_USER_DIR="$HOME/.local/share/flatpak" setsid "$HERE/run.sh" flatpak run --env=ACCESSIBILITY_ENABLED=1 "$@" \
    org.chromium.Chromium --user-data-dir="$DIR/profile" --no-first-run \
    --no-default-browser-check --password-store=basic --disable-sync \
    --force-renderer-accessibility $CHROMIUM_ARGS \
    "file://$DIR/${CHROMIUM_PAGE:-page.html}" > "$STATE/app-chromium.log" 2>&1 &
