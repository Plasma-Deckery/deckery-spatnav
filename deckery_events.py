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

Context-aware scheduling (deckery#28): all internal timers/idle callbacks go
through _add_timeout()/_add_idle() instead of the GLib.timeout_add()/
GLib.idle_add() convenience wrappers. Those wrappers ALWAYS attach to the
global-default main context (the thread that owns it — typically the GTK
main thread) regardless of which thread calls them; a bare
`context.push_thread_default()` on a worker thread does NOT redirect them
(measured directly — see debug_worker_context_test.py). Explicit
GLib.Source + .attach(context) is the only way to actually schedule
callbacks on a specific non-default thread's loop. Passing context=None to
start() (prototype_atspi_overlay.py's usage — everything on the GTK main
thread) reproduces the old behavior exactly, since Source.attach(None)
means "the global-default context" too. deckery_backend.py passes its
worker thread's own context instead, so all AT-SPI event dispatch + timers
here run there, never touching the GTK main thread.
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
# Tested at 2s (deckery#28) to see whether a frequent periodic re-walk could
# paper over the Qt/QML scroll-tracking gap (see REQUIREMENTS.md / deckery
# session notes on QQuickListView not emitting any position-change
# accessibility event). Measured live: a single build_initial() pass took
# 14-21 SECONDS for 37 windows / 212 elements on this system — at a 2s
# interval, each rebuild's background thread was still running when the next
# one started, piling up unboundedly with no cap on concurrent
# threading.Thread(daemon=True) rebuilds. A periodic full re-walk is not a
# viable fix at any frequency remotely useful for scroll-tracking; whatever
# solves that needs to cost a small, bounded fraction of a full walk (see the
# sibling-canary polling idea under discussion), not scale the walk itself
# down in frequency. Back to 60s as a pure "recover from missed events"
# safety net, its original purpose.
FULL_REBUILD_SECONDS = 60

# Active-window poll interval (deckery#28). Qt/QML content never fires an
# AT-SPI event for a plain position change (confirmed: QQuickListView calls
# no accessibility update on scroll; Qt's Linux bridge drops
# QAccessible::LocationChanged as a no-op regardless) — there is structurally
# no event to catch that with. Scoped to the focused window only (not a full
# desktop poll): measured live, the focused window's already-known element
# count is typically 13-93 on this system vs. 200+ across the whole desktop
# — cheap enough to force-refresh on a short timer, unlike a full re-walk
# (14-21s, see FULL_REBUILD_SECONDS above) or an unscoped full-desktop poll.
ACTIVE_WINDOW_POLL_MS = 200


class EventRouter:
    """Registers AT-SPI event listeners and routes to an ElementCache."""

    def __init__(self, cache):
        self._cache = cache
        self._spam_filter = EventSpamFilter()
        self._listener = None
        self._on_change = None
        self._render_pending = False  # debounce: at most one queued render
        self._context = None  # which GLib.MainContext our timers/idles attach to

    def _add_timeout(self, interval_ms, callback):
        """GLib.Source-based equivalent of GLib.timeout_add(), attached to
        self._context instead of always the global-default context."""
        src = GLib.timeout_source_new(interval_ms)
        src.set_callback(callback)
        src.attach(self._context)
        return src

    def _add_idle(self, callback):
        """GLib.Source-based equivalent of GLib.idle_add(), attached to
        self._context instead of always the global-default context."""
        src = GLib.idle_source_new()
        src.set_callback(callback)
        src.attach(self._context)
        return src

    def start(self, on_change, context=None):
        """Register listeners. context is the GLib.MainContext all of this
        router's timers/idle callbacks (and therefore on_change() itself,
        when triggered by them) run on — None means the global-default
        context, i.e. the GTK main thread that's calling this. Pass a
        worker thread's own context to keep everything here, including
        on_change(), off the GTK main thread (deckery_backend.py)."""
        self._on_change = on_change
        self._context = context
        self._listener = Atspi.EventListener.new(self._on_event)
        for event_type in EVENT_TYPES:
            self._listener.register(event_type)
        print(f"[events] registered AT-SPI listener for {len(EVENT_TYPES)} event types")
        self._add_timeout(FULL_REBUILD_SECONDS * 1000, self._periodic_rebuild)
        self._add_timeout(ACTIVE_WINDOW_POLL_MS, self._poll_active_window)

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
        elif t.startswith("object:children-changed:add"):
            self._cache.on_children_changed_add(event)
        elif t.startswith("object:children-changed:remove"):
            self._cache.on_children_changed_remove(event)
        elif t in ("object:state-changed:showing", "object:state-changed:enabled"):
            self._cache.on_state_changed(event)

        self._schedule_change()

    def _schedule_change(self):
        """Coalesce rapid AT-SPI events into a single render via idle_add.
        Without this, Firefox building its tree fires thousands of events
        and each one triggers a full render, blocking the GTK main loop."""
        if self._render_pending or self._on_change is None:
            return
        self._render_pending = True
        self._add_idle(self._fire_change)

    def _fire_change(self):
        self._render_pending = False
        if self._on_change is not None:
            self._on_change()
        return False

    def _poll_active_window(self):
        """Force-refresh the focused window's cached extents/name/role on a
        short timer, to bound staleness for position changes that fire no
        AT-SPI event at all (deckery#28 — see ACTIVE_WINDOW_POLL_MS above
        and ElementCache.invalidate_window_extents()'s docstring). Runs on
        whichever context this router was start()ed with — no separate
        background thread of its own, no risk of the pile-up measured for
        FULL_REBUILD_SECONDS at short intervals."""
        h = self._cache.active_window_hash
        if h is not None:
            self._cache.invalidate_window_extents(h)
            self._schedule_change()
        return True  # repeat

    def _periodic_rebuild(self):
        """Safety-net: trigger a full rebuild in case events were missed."""
        threading.Thread(target=self._rebuild_worker, daemon=True).start()
        return True

    def _rebuild_worker(self):
        t0 = time.time()
        with ATSPI_LOCK:
            meta, elements = self._cache.build_initial()
        elapsed_ms = (time.time() - t0) * 1000
        n_elements = sum(len(v) for v in elements.values())
        print(f"[events] {self._ts()} periodic rebuild: {elapsed_ms:.0f}ms, "
              f"{len(meta)} windows, {n_elements} elements")
        self._add_idle(lambda: self._apply_rebuild(meta, elements))

    def _apply_rebuild(self, meta, elements):
        self._cache.apply_initial(meta, elements)
        if self._on_change is not None:
            self._on_change()
        return False
