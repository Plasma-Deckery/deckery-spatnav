#!/bin/bash
# Runs (as root) shortly after every resume from suspend, launched detached
# by the system-sleep hook in system-sleep-kwin-hang-watchdog. Pings
# kwin_wayland's D-Bus peer; if it doesn't answer within ~12s, captures
# kernel-side thread stacks, an optional gdb backtrace, and a sysrq task
# dump, so a repeat of the black-screen/audio-loop resume hang can be
# root-caused from the captured files instead of having to catch it live.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT_ROOT="$SCRIPT_DIR/hang-reports"
mkdir -p "$REPORT_ROOT"
LOG="$REPORT_ROOT/watchdog.log"
MAX_REPORTS=15   # keep at most this many captured incidents on disk

# Keep the one-line log itself from growing forever (it gets one line
# per resume, healthy or not, so this is a belt-and-suspenders cap).
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Prune old incident captures beyond MAX_REPORTS (directories are named
# by timestamp, so plain sort gives oldest-first).
prune_old_reports() {
  local dirs
  mapfile -t dirs < <(find "$REPORT_ROOT" -maxdepth 1 -mindepth 1 -type d | sort)
  local excess=$(( ${#dirs[@]} - MAX_REPORTS ))
  if [ "$excess" -gt 0 ]; then
    for ((i = 0; i < excess; i++)); do
      rm -rf "${dirs[$i]}"
    done
  fi
}
prune_old_reports

TARGET_USER="${SUDO_USER:-philipp}"
RUNTIME_DIR="/run/user/$(id -u "$TARGET_USER")"
BUS="unix:path=$RUNTIME_DIR/bus"

log() { echo "$(date -Is) $*" >> "$LOG"; }

ping_kwin() {
  timeout 2 sudo -u "$TARGET_USER" env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="$BUS" \
    busctl --user call org.kde.KWin /KWin org.freedesktop.DBus.Peer Ping \
    >/dev/null 2>&1
}

RESUME_EPOCH="${1:-$(date +%s)}"
log "resume detected (epoch $RESUME_EPOCH), starting watchdog"

# Give kwin a moment to come back on its own before judging it hung.
HEALTHY=0
i=0
for i in $(seq 1 6); do
  sleep 2
  if ping_kwin; then
    HEALTHY=1
    break
  fi
done

if [ "$HEALTHY" = "1" ]; then
  log "kwin responded after ~$((i * 2))s — resume OK, no capture needed"
  exit 0
fi

HANG_START="$(date -Is)"
DIR="$REPORT_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DIR"
log "kwin unresponsive after 12s — capturing diagnostics into $DIR"

KWIN_PID="$(pgrep -x kwin_wayland | head -1)"
echo "kwin_pid=$KWIN_PID resume_epoch=$RESUME_EPOCH hang_detected=$HANG_START" > "$DIR/meta.txt"

if [ -n "$KWIN_PID" ]; then
  {
    echo "== /proc/$KWIN_PID/status =="
    cat "/proc/$KWIN_PID/status" 2>&1
    for t in /proc/"$KWIN_PID"/task/*/; do
      tid="$(basename "$t")"
      echo
      echo "== thread $tid wchan =="
      cat "$t/wchan" 2>&1; echo
      echo "== thread $tid kernel stack =="
      cat "$t/stack" 2>&1
    done
  } > "$DIR/kernel-stacks.txt" 2>&1

  if command -v gdb >/dev/null 2>&1; then
    # Hard timeout + size cap: "bt full" dumps locals and could in
    # principle be large or (if gdb itself wedges on a frozen process)
    # hang indefinitely.
    timeout 30 gdb -p "$KWIN_PID" -batch \
      -ex "set pagination off" \
      -ex "thread apply all bt full" \
      -ex detach \
      2>&1 | head -c 2000000 > "$DIR/gdb-backtrace.txt"
  else
    echo "gdb not installed — skipped (install with: brew install gdb)" > "$DIR/gdb-backtrace.txt"
  fi
fi

# Kernel-wide blocked-task dump: shows whether amdgpu/DRM itself is stuck
# in the kernel, not just kwin's userspace thread. Capped by line count
# in case the system is extra chatty right at this moment.
echo t > /proc/sysrq-trigger 2>/dev/null
sleep 1
journalctl -k --since "-5 seconds" -n 5000 > "$DIR/sysrq-task-dump.txt" 2>&1

journalctl -b 0 --since "@$((RESUME_EPOCH - 5))" --until "+5 seconds" -n 5000 \
  > "$DIR/journal-context.txt" 2>&1

# Re-prune in case this new capture pushed us over the limit.
prune_old_reports

log "diagnostics captured in $DIR, continuing to watch for recovery"

# Keep polling so we know how long the freeze actually lasted, and to
# timestamp the recovery for correlation with the journal.
for i in $(seq 1 100); do
  sleep 3
  if ping_kwin; then
    log "kwin recovered after ~$((12 + i * 3))s total — see $DIR"
    echo "recovered_after_seconds=$((12 + i * 3))" >> "$DIR/meta.txt"
    exit 0
  fi
done

log "kwin still unresponsive after 5+ minutes — giving up watching, diagnostics in $DIR"
