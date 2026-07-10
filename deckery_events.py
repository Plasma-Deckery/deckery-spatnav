#!/usr/bin/env python3
"""
deckery_events.py — AT-SPI event listener, routes to ElementCache.

Registers Atspi.EventListener for the events that mean "something in the
UI changed" and routes each one to the appropriate ElementCache handler
(deckery_cache.py). The cache does O(1) or O(subtree) work per event —
NOT a full app re-walk.

Orca-style: event.any_data is the specific changed object. We touch only
that object and its immediate subtree, not the entire owning application.

Doesn't need Atspi.event_main(): runs inside a GTK app that already pumps
the default GLib main loop. AT-SPI GI bindings dispatch incoming D-Bus event
signals through that same main context.
"""

import threading
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from deckery_event_filter import EventSpamFilter
from deckery_cache import ATSPI_LOCK

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

# Safety-net: full cache rebuild every N seconds in case events were missed.
FULL_REBUILD_SECONDS = 60


class EventRouter:
    """Registers AT-SPI event listeners and routes to an ElementCache."""

    def __init__(self, cache):
        self._cache = cache
        self._spam_filter = EventSpamFilter()
        self._listener = None
        self._on_change = None

    def start(self, on_change):
        """Register listeners. Must be called from the GTK main thread."""
        self._on_change = on_change
        self._listener = Atspi.EventListener.new(self._on_event)
        for event_type in EVENT_TYPES:
            self._listener.register(event_type)
        print(f"[events] registered AT-SPI listener for {len(EVENT_TYPES)} event types")
        GLib.timeout_add_seconds(FULL_REBUILD_SECONDS, self._periodic_rebuild)

    @staticmethod
    def _ts():
        t = time.time()
        return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"

    def _on_event(self, event: Atspi.Event) -> None:
        if self._spam_filter.should_ignore(event):
            return

        print(f"[events] {self._ts()} event: {event.type}")

        t = event.type
        if t == "window:create":
            self._cache.on_window_create(event)
        elif t == "window:activate":
            # Re-walk only if the window isn't already in the cache
            # (catches windows that appear without a window:create event).
            # If already cached, skip the render — the KWin tracker update
            # will trigger it when the new desktop's visible_pids arrive.
            if not self._cache.on_window_activate(event):
                return
        elif t == "window:destroy":
            self._cache.on_window_destroy(event)
        elif t == "window:deactivate":
            pass  # window still exists, nothing to do in the cache
        elif t == "object:children-changed:add":
            self._cache.on_children_changed_add(event)
        elif t == "object:children-changed:remove":
            self._cache.on_children_changed_remove(event)
        elif t in ("object:state-changed:showing", "object:state-changed:enabled"):
            self._cache.on_state_changed(event)

        if self._on_change is not None:
            self._on_change()

    def _periodic_rebuild(self):
        """Safety-net: trigger a full rebuild in case events were missed."""
        threading.Thread(target=self._rebuild_worker, daemon=True).start()
        return True

    def _rebuild_worker(self):
        with ATSPI_LOCK:
            meta, elements = self._cache.build_initial()
        GLib.idle_add(self._apply_rebuild, meta, elements)

    def _apply_rebuild(self, meta, elements):
        self._cache.apply_initial(meta, elements)
        if self._on_change is not None:
            self._on_change()
        return False
