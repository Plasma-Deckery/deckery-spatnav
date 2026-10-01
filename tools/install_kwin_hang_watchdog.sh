#!/bin/bash
# Run this once, locally on the Deck, in a real terminal (not through a
# sandboxed/containerized shell) -- it needs genuine root on the host to
# register the systemd-sleep hook.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

chmod +x "$REPO_DIR/tools/kwin_hang_watchdog.sh"

sudo mkdir -p /etc/systemd/system-sleep
sudo install -m 0755 "$REPO_DIR/tools/system-sleep-kwin-hang-watchdog" \
  /etc/systemd/system-sleep/99-kwin-hang-watchdog

echo "Installed: /etc/systemd/system-sleep/99-kwin-hang-watchdog"
echo "It fires automatically on every resume from suspend from now on."
echo "Reports (only written if a hang is actually detected) land in:"
echo "  $REPO_DIR/tools/hang-reports/"
echo "A one-line healthy/recovered log is kept at:"
echo "  $REPO_DIR/tools/hang-reports/watchdog.log"
