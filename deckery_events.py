#!/usr/bin/env python3
"""
deckery_events.py — event-driven AT-SPI tree cache (deckery#28).

This is the "stop rebuilding the whole tree on a timer, do it the way a
real AT-SPI client (Orca) does it" piece: register Atspi.EventListener for
the handful of event types that mean "some app's tree actually changed"
(children-changed, state-changed:showing/enabled, window create/destroy/
activate/deactivate), filter out flood/spam via deckery_event_filter's
EventSpamFilter (adapted from GNOME Orca's event_manager.py — see that
module's docstring for what's ported vs. reimplemented), and re-walk only
the ONE app the event came from via deckery_walk.walk_app() — not the whole
desktop.

Doesn't need Atspi.event_main(): this runs inside a GTK app that already
pumps the default GLib main loop (Gtk.Application.run()), and Atspi's GI
bindings dispatch incoming D-Bus event signals through that same default
main context — event_main()/event_quit() are only needed for a bare Atspi
client with no GLib main loop of its own (e.g. a plain script).

TreeCache.tree is read directly by deckery_walk.render_tree() — this module
only maintains it, doesn't decide what's visible/on-screen (that split is
explained in deckery_walk.py's docstring).
"""

import threading
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

import deckery_walk as walk
from deckery_event_filter import EventSpamFilter
from vendor.orca_ax.ax_object import AXObject

# The AT-SPI event types worth re-walking an app for. Deliberately narrow:
# object:children-changed catches elements appearing/disappearing (dialogs,
# popups, dynamically loaded content), state-changed:showing/enabled catches
# our FOCUSABLE+SHOWING+ENABLED filter's inputs flipping without a
# structural tree change, and the window:* events catch whole windows
# appearing/closing/gaining focus (a new window is a children-changed event
# on its Application in principle, but window:create fires more reliably
# across toolkits per Orca's own event registration — see
# event_manager.py's default listener set for the same rationale).
EVENT_TYPES = (
    "object:children-changed:add",
    "object:children-changed:remove",
    "object:state-changed:showing",
    "object:state-changed:enabled",
    "window:create",
    "window:destroy",
    "window:activate",
    "window:deactivate",
)

# Safety net: even a well-behaved event-driven cache can miss updates (a
# toolkit that doesn't fire the right AT-SPI event for some change, an app
# that registers late, an event lost to the spam filter's dedup window at
# exactly the wrong moment). Rebuilding the whole tree this rarely is cheap
# relative to how often it used to happen (every REFRESH_SECONDS == 2s) and
# also naturally prunes stale entries for apps that closed without a clean
# window:destroy for every window.
FULL_REBUILD_SECONDS = 30


class TreeCache:
    """Owns the live AT-SPI tree: one AppEntry per app, kept current via
    AT-SPI events instead of periodic re-walks. Not thread-safe by itself —
    see start()'s threading notes."""

    def __init__(self):
        self.tree = {}
        self._spam_filter = EventSpamFilter()
        self._listener = None
        self._on_change = None
        self._walk_in_progress = threading.Event()
        # Cap how long any single AT-SPI D-Bus call may block — without a
        # timeout, a call into a Chromium app whose AT-SPI bridge is
        # disconnected (e.g. after suspend/resume) blocks forever, keeping
        # ATSPI_LOCK held and eventually freezing the GTK main loop (see
        # _walk_and_apply below).  2 s is long enough for a slow app to
        # respond but short enough to unblock the background thread promptly.
        Atspi.set_timeout(2000, 2000)

    def build_full(self):
        """Synchronous, blocking full-desktop walk — same cost as the old
        do_walk()'s AT-SPI half. Call this off the GTK main thread (see
        prototype_atspi_overlay.py's startup sequence) since it can take
        1-13+ seconds exactly like the walk it replaces."""
        self.tree = walk.build_full_tree()

    def start(self, on_change):
        """Registers the AT-SPI event listener. Must be called from the
        thread that owns the GLib main loop the events will be dispatched
        through (i.e. the GTK main thread) — call after build_full() has
        already populated self.tree from a background thread.

        `on_change` is called (no args, on the GTK main thread — event
        dispatch already happens there, see module docstring) after
        self.tree has been updated in response to a real event."""
        self._on_change = on_change
        self._listener = Atspi.EventListener.new(self._on_event)
        for event_type in EVENT_TYPES:
            self._listener.register(event_type)
        print(f"[events] registered AT-SPI listener for {len(EVENT_TYPES)} event types")

        # Full-desktop safety net (see FULL_REBUILD_SECONDS) — runs on a
        # long interval on the GTK main thread's timer, but the walk itself
        # still happens on a background thread so it can't freeze the
        # overlay the way a blocking multi-second AT-SPI walk would.
        GLib.timeout_add_seconds(FULL_REBUILD_SECONDS, self._periodic_full_rebuild)

    @staticmethod
    def _ts() -> str:
        """Current wall-clock time as HH:MM:SS.mmm for latency logging."""
        t = time.time()
        ms = int((t % 1) * 1000)
        return time.strftime("%H:%M:%S", time.localtime(t)) + f".{ms:03d}"

    def _on_event(self, event: Atspi.Event) -> None:
        if self._spam_filter.should_ignore(event):
            return

        print(f"[events] {self._ts()} event: {event.type}")

        # Hand event.source off to a background thread immediately — do NOT
        # call get_application() (an AT-SPI call) here on the GTK main thread.
        # If a background walk thread is holding ATSPI_LOCK (e.g. blocked on a
        # slow/dead Chromium AT-SPI connection), acquiring it here would freeze
        # the GTK main loop, killing tracker updates, rendering, everything.
        # Moving all AT-SPI calls into the background thread means a stalled
        # call only blocks that one thread, not the main loop.
        threading.Thread(
            target=self._walk_and_apply, args=(event.source,), daemon=True
        ).start()

    def _walk_and_apply(self, source: Atspi.Accessible) -> None:
        # All AT-SPI calls run under ATSPI_LOCK in this background thread.
        # get_application() and walk_app() are held under the same lock
        # acquisition (RLock, so walk_app()'s own re-acquisition on the same
        # thread is fine) to prevent any gap where concurrent threads could
        # race on libatspi. If the call hangs (stale Chromium AT-SPI
        # connection etc.) Atspi.set_timeout() above caps the wait at 2 s.
        try:
            with walk.ATSPI_LOCK:
                app = Atspi.Accessible.get_application(source)
                if app is None:
                    return
                entry = walk.walk_app(app)
        except Exception:  # noqa: BLE001
            return
        GLib.idle_add(self._apply_app_entry, app, entry)

    def _apply_app_entry(self, app: Atspi.Accessible, entry) -> bool:
        print(f"[events] {self._ts()} walk applied ({len(entry.windows)} windows)")
        self.tree[hash(app)] = entry
        if self._on_change is not None:
            self._on_change()
        return False  # one-shot idle source

    def _periodic_full_rebuild(self) -> bool:
        threading.Thread(target=self._full_rebuild_worker, daemon=True).start()
        return True  # keep the timeout alive

    def _full_rebuild_worker(self) -> None:
        new_tree = walk.build_full_tree()
        GLib.idle_add(self._apply_full_tree, new_tree)

    def _apply_full_tree(self, new_tree) -> bool:
        self.tree = new_tree
        if self._on_change is not None:
            self._on_change()
        return False  # one-shot idle source
