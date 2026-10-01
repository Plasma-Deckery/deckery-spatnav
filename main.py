#!/usr/bin/env python3
"""
main.py — single entry point for the deckery spatial-nav prototype.

Owns exactly one SharedBackend (deckery_backend.py) and starts the
CursorNavWindow frontend (cursor_nav_prototype.py) against it. This exists
specifically so there is only ever ONE process doing AT-SPI work: running
the detection overlay and the navigation prototype as two independent
processes (each building its own ElementCache/WindowTracker) meant they
fought over the single-owner org.deckery.WindowTracker D-Bus name, and
whichever one lost silently fell back to unfiltered (stopped excluding
elements on other virtual desktops) — a real correctness bug, not just
wasted resources. See deckery_backend.py's docstring for the full story.

Adding a second frontend later (e.g. reviving the plain box-only detection
view) means constructing it here against the same `backend` instance, not
spawning another process.

Single-instance lock: iterating on this via distrobox + a tray + manual
`python3 -u main.py` launches in the same debugging session repeatedly left
MULTIPLE live copies running at once (each with its own ElementCache,
racing for org.deckery.WindowTracker/CursorNav) — exactly the confusion
this whole architecture was meant to eliminate, just one process up instead
of two. acquire_single_instance_lock() below uses flock() on a PID file
under XDG_RUNTIME_DIR, not a plain "does a PID file exist" check — flock is
released automatically by the kernel even if the holder crashes or gets
kill -9'd, so a stale file from a dead process can never block a new
launch (the classic failure mode of hand-rolled PID-file locking). A second
launch while one is already live exits immediately instead of limping
along in a degraded, hard-to-debug two-process state.

Run: distrobox enter deckery -- python3 main.py
"""

import fcntl
import os
import sys

_LAYER_SHELL_PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if _LAYER_SHELL_PRELOAD not in os.environ.get("LD_PRELOAD", "").split(":"):
    existing = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = (
        f"{_LAYER_SHELL_PRELOAD}:{existing}" if existing else _LAYER_SHELL_PRELOAD
    )
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(sys.executable, [sys.executable] + sys.argv)

_LOCK_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "deckery-cursor-nav.lock")
# Module-level so the fd (and thus the flock) stays held for the process's
# whole lifetime — flock releases automatically once every fd referencing
# it is closed, which happens on process exit (including a crash/SIGKILL)
# even if we never explicitly close it ourselves.
_lock_file = None


def acquire_single_instance_lock():
    global _lock_file
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[lock] another instance already holds {_LOCK_PATH} — exiting.")
        sys.exit(1)
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()


acquire_single_instance_lock()

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Atspi", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gtk

from deckery_backend import SharedBackend
from cursor_nav_prototype import CursorNavWindow


def main():
    backend = SharedBackend()
    app = Gtk.Application(application_id="dev.deckery.main")

    def on_activate(app):
        win = CursorNavWindow(backend)
        win.set_application(app)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
