#!/usr/bin/env python3
"""
deckery_backend.py — single shared AT-SPI backend for all deckery frontends.

Exists to fix a real architecture bug (deckery#28 session notes): running
prototype_atspi_overlay.py and cursor_nav_prototype.py as two independent
processes meant each one built its OWN ElementCache/WindowTracker — two
processes doing the same AT-SPI walk, fighting over the single-owner
org.deckery.WindowTracker D-Bus name, and (the actual correctness bug, not
just waste) whichever one lost that race fell back to "unfiltered" and
silently stopped excluding elements on other virtual desktops. One backend,
one process, any number of frontends reading from it — not one backend per
frontend.

SharedBackend owns:
  - ElementCache (the AT-SPI element data)
  - WindowTracker (KWin virtual-desktop/offset data)
  - EventRouter (deckery_events.py — AT-SPI event listening + the
    active-window-scoped poll + the 60s full-rebuild safety net)
  - enable_chromium_accessibility() (one-shot Chromium/Firefox a11y trigger)

All AT-SPI/libatspi calls happen on ONE dedicated worker thread running its
OWN GLib.MainContext/MainLoop — not the GTK main thread, and not the
global-default context either. Earlier version of this file ran a plain
`while True: render_tree(); sleep(0.25)` loop here instead of EventRouter,
which had two real bugs found live (deckery#28 session notes, screenshot-
confirmed): (1) the element SET froze at whatever build_initial() saw at
startup — render_tree() only recomputes positions of already-known
elements, it discovers nothing new, so windows/menus/list rows that
appeared afterward (e.g. Kirigami ListView delegates scrolled into view,
recreated systray icons) never showed a box, while their now-dead old
references got silently skipped; (2) the poll's 250ms interval was shorter
than ElementCache's 500ms extents TTL, so different elements in the same
render reflected different scroll moments — a visibly torn frame. Wiring in
EventRouter fixes (1) (it's the thing that keeps the element set current)
and (2) (invalidate_window_extents() forces a true fresh fetch every poll
tick instead of relying on TTL expiry).

Why a dedicated GLib.MainContext instead of just calling EventRouter.start()
here: GLib.idle_add()/GLib.timeout_add() — what EventRouter used internally
before this change — always attach to the global-default main context
(normally the GTK main thread) regardless of which thread calls them; a
thread merely doing context.push_thread_default() does NOT redirect them
(measured directly, see debug_worker_context_test.py). EventRouter now
takes an explicit `context` argument and uses GLib.Source + .attach(context)
throughout, so passing this worker thread's own context keeps its event
listener, active-window poll, and full-rebuild timer — and therefore every
render they trigger — off the GTK main thread. Only the final candidates
list crosses back over, via a plain (context-less, i.e. global-default)
GLib.idle_add() in _apply_candidates().
"""

import threading

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import GLib

import deckery_walk as walk
from deckery_cache import ElementCache
from deckery_events import EventRouter
from deckery_a11y import enable_chromium_accessibility
from deckery_tracker import WindowTracker


class SharedBackend:
    """One instance per process. Construct it once, call start(), then read
    .candidates (list of (role, name, extents)) and .tree_ready from the
    GTK main thread at any time — both are only ever written via a
    context-less GLib.idle_add() (i.e. targeting the global-default/GTK
    main-thread context), so reading them from the main thread is always
    safe."""

    def __init__(self):
        self.element_cache = ElementCache()
        # on_update=self._on_tracker_update (deckery#28 — was None): the
        # active-window poll alone leaves a real gap right after startup.
        # active_window_hash stays None until the first window:activate
        # fires, so the poll's `if h is not None` guard does nothing until
        # then — confirmed live: the very first render (fired once,
        # unconditionally, right after EventRouter.start()) runs BEFORE the
        # push-on-start tracker data has arrived, so it's unfiltered; with
        # no on_update wired in, NOTHING re-rendered after that until the
        # user happened to trigger an AT-SPI event manually (e.g. switching
        # windows), even though the tracker itself had good data the whole
        # time. Wiring on_update closes that gap: any fresh KWin push
        # (including our own immediate one) now triggers a render.
        self.tracker = WindowTracker(on_update=self._on_tracker_update)
        self.event_router = EventRouter(self.element_cache)
        self.candidates = []  # list of (role, name, extents)
        self.tree_ready = False
        self._on_candidates = None  # optional extra callback, see start()
        self._worker_context = None

    def start(self, on_candidates=None):
        """Begins the worker thread: its own GLib main loop, the one-time
        initial walk, then EventRouter (event listener + active-window
        poll + full-rebuild safety net). on_candidates(candidates), if
        given, is called on the GTK main thread every time self.candidates
        is refreshed — frontends that need to react (recompute nav state,
        queue a redraw) hook in here instead of polling this object
        themselves."""
        self._on_candidates = on_candidates
        threading.Thread(target=self._worker_main, daemon=True).start()

    def _worker_main(self):
        """Runs entirely on a dedicated thread with its own GLib main loop
        — see module docstring for why. Everything AT-SPI-related
        (tracker, initial walk, EventRouter, every render it triggers)
        happens here; the GTK main thread never touches AT-SPI."""
        context = GLib.MainContext.new()
        context.push_thread_default()
        self._worker_context = context

        self.tracker.start()
        enable_chromium_accessibility()

        meta, elements = self.element_cache.build_initial()
        self.element_cache.apply_initial(meta, elements)
        self.tree_ready = True

        self.event_router.start(on_change=self._render_and_publish, context=context)
        self._render_and_publish()  # initial render, don't wait for the first event

        GLib.MainLoop(context).run()

    def _on_tracker_update(self):
        """WindowTracker's on_update — fires whenever fresh KWin data
        arrives, including deckery_tracker.py's own push-on-start request
        (deckery#28). We don't know which thread the underlying D-Bus
        callback landed on (the same uncertainty documented in
        deckery_events.py's module docstring), so schedule the actual
        render via an explicit Source attached to the worker context rather
        than calling it directly — keeps this off the GTK main thread
        regardless."""
        if self._worker_context is None:
            return
        src = GLib.idle_source_new()
        src.set_callback(lambda: (self._render_and_publish(), False)[1])
        src.attach(self._worker_context)

    def _render_and_publish(self):
        """EventRouter's on_change callback — runs on the worker thread
        (see class docstring). Computes candidates, then hands the final
        list to the GTK main thread via a context-less idle_add."""
        result = walk.render_tree(self.tracker, self.element_cache)
        candidates = [
            (role, name, extents)
            for _, role, name, extents in result.elements
            if extents is not None and extents.width > 0 and extents.height > 0
        ]
        GLib.idle_add(self._apply_candidates, candidates)

    def _apply_candidates(self, candidates):
        self.candidates = candidates
        if self._on_candidates is not None:
            self._on_candidates(candidates)
        return False
