#!/bin/sh
# Run a HOST command inside the test desktop: tools/testdesk/run.sh dolphin
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk"
[ -s "$STATE/env" ] || { echo "test desktop not running"; exit 1; }
. "$STATE/env"
unset AT_SPI_BUS_ADDRESS SESSION_MANAGER XAUTHORITY
exec "$@"
