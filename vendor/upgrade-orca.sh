#!/usr/bin/env bash
# vendor/upgrade-orca.sh — sync vendor/orca_ax/ with upstream Orca.
#
# Usage:
#   ./vendor/upgrade-orca.sh          # show what changed upstream
#   ./vendor/upgrade-orca.sh --apply  # copy upstream files into vendor/orca_ax/
#                                     # then review + re-apply our patches manually
#
# How it works:
#   vendor/orca/ is a git submodule (sparse checkout of 6 files from
#   github.com/GNOME/orca). vendor/orca_ax/ is our adapted copy of those
#   files with two patches applied:
#     - ax_utilities_state.py: removed get_current_item_status_string() and
#       its `messages` import (messages.py pulls in orca_i18n + orca_platform,
#       a deep Orca-internal dependency chain we don't need).
#     - debug.py: replaced AXUtilitiesDebugging.as_string() with plain str()
#       (AXUtilitiesDebugging pulls in ax_action, ax_utilities_application,
#       ax_utilities_relation — again not needed for our AT-SPI access layer).
#
# To upgrade to a newer Orca commit:
#   1. git submodule update --remote vendor/orca   # pull latest upstream
#   2. ./vendor/upgrade-orca.sh                    # review the diff
#   3. ./vendor/upgrade-orca.sh --apply            # copy upstream into orca_ax/
#   4. Re-apply the two patches above (or check if upstream fixed them)
#   5. Test the overlay, then commit vendor/orca + vendor/orca_ax/ together

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM="$SCRIPT_DIR/orca/src/orca"
ADAPTED="$SCRIPT_DIR/orca_ax"
FILES=(ax_object.py ax_component.py ax_utilities_state.py ax_cache_manager.py debug.py)

if [ ! -f "$UPSTREAM/ax_object.py" ]; then
    echo "error: vendor/orca submodule not checked out — run: git submodule update --init vendor/orca"
    exit 1
fi

echo "=== upstream Orca commit ==="
git -C "$SCRIPT_DIR/orca" log -1 --oneline
echo

any_diff=0
for f in "${FILES[@]}"; do
    result=$(diff "$UPSTREAM/$f" "$ADAPTED/$f" || true)
    if [ -n "$result" ]; then
        echo "=== diff: $f ==="
        echo "$result"
        echo
        any_diff=1
    fi
done

if [ "$any_diff" -eq 0 ]; then
    echo "vendor/orca_ax/ is in sync with upstream (modulo expected patches)."
fi

if [ "${1:-}" = "--apply" ]; then
    echo "--- copying upstream files into vendor/orca_ax/ ---"
    for f in "${FILES[@]}"; do
        cp "$UPSTREAM/$f" "$ADAPTED/$f"
        echo "  copied $f"
    done
    echo
    echo "Done. Now re-apply the two deckery patches (see script header) and test."
fi
