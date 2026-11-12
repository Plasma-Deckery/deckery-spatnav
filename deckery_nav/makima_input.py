"""Makima's input as it already is: the analog values in makima-state.json.

TEMPORARY WORKAROUND, for testing only (makima owner + user, 2026-11-12):
reading state.json and switching the export on over IPC is not the way
spatnav gets its input long-term — that will be a dedicated channel (makima
backlog B-13, NAV_PROTOTYPE.md "Vorschlag Makima-Brücke"). Build nothing
permanent on this module; replace it when the channel exists.

No makima change needed: with `analog-state-export on` makima writes stick
and pad positions into $XDG_RUNTIME_DIR/makima-state.json on every change
(≥ 0.0045 apart, up to ~90/s). The prototype uses only what the deckery
config leaves free — left stick (no LSTICK function) and left pad
(mode "disabled").

The export is only on while the HUD shows the pads. Agreed with the makima
owner (2026-11-12): we may switch it on, at most once per second; on exit
we switch it off again unless the HUD is open (makima is paused while the
full HUD is open — the only sign of it we can read)."""

import json
import os
import socket
from dataclasses import dataclass


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


STATE_PATH = os.path.join(runtime_dir(), "makima-state.json")
CONTROL_SOCKET = os.path.join(runtime_dir(), "makima-control.sock")


@dataclass(frozen=True)
class Pad:
    x: float = 0.0
    y: float = 0.0
    touching: bool = False
    pressed: bool = False


@dataclass(frozen=True)
class InputSnapshot:
    lpad: Pad = Pad()
    lstick: tuple = (0.0, 0.0)
    analog: bool = False        # makima is exporting analog values
    paused: bool = False


def parse_state(state):
    """makima-state.json (parsed) → InputSnapshot. Pads/sticks are null on
    writes that carry no analog values (pause/resume)."""
    ctx = state.get("context") or {}
    pads = state.get("trackpads") or {}
    sticks = state.get("sticks") or {}
    left = pads.get("left") or {}
    lstick = sticks.get("lstick") or {}
    return InputSnapshot(
        lpad=Pad(float(left.get("x", 0.0)), float(left.get("y", 0.0)),
                 bool(left.get("touching", False)), bool(left.get("pressed", False))),
        lstick=(float(lstick.get("x", 0.0)), float(lstick.get("y", 0.0))),
        analog=bool(ctx.get("analog_state_export", False)) and bool(pads),
        paused=bool(ctx.get("paused", False)),
    )


class StateFile:
    """Re-reads the file when its mtime changes (makima replaces it
    atomically). Call poll() at the input rate."""

    def __init__(self, path=STATE_PATH):
        self.path = path
        self._mtime = None
        self.snapshot = InputSnapshot()

    def poll(self):
        """True when a new snapshot was read."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            with open(self.path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return False   # caught mid-write: next poll
        self._mtime = mtime
        self.snapshot = parse_state(state)
        return True


@dataclass
class ExportKeeper:
    """When to send `analog-state-export on/off` (pure).
    TEMPORARY WORKAROUND for testing — see the module docstring."""
    min_interval: float = 1.0
    last_sent: float = None

    def want_on(self, snapshot, now):
        if snapshot.analog:
            return False
        if self.last_sent is not None and now - self.last_sent < self.min_interval:
            return False
        self.last_sent = now
        return True

    def want_off_at_exit(self, snapshot):
        return self.last_sent is not None and not snapshot.paused


def makima_command(cmd, path=CONTROL_SOCKET):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(path)
            s.sendall((cmd + "\n").encode())
        return True
    except OSError:
        return False
