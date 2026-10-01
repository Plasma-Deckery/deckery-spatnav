#!/usr/bin/env python3
"""
deckery_cache.py — Orca-style incremental element cache.

Replaces the old AppEntry/WindowEntry/TreeCache design. Instead of
re-walking an entire app on every AT-SPI event (O(n*m) D-Bus calls),
we maintain a flat dict of which Atspi.Accessible objects are currently
relevant (focusable+showing+enabled), keyed by their containing window.

Caching: the module previously assumed AXComponent.get_rect() alone was a
live D-Bus call while AXObject.get_name()/get_role_name()/get_state_set()
were served from libatspi's own protocol cache after build_initial(). That
assumption was wrong for all three — measured directly on this system
(bench_render.py, deckery#28): get_rect() ~450-730 µs/call, get_name()
~520 µs/call, get_role_name() ~615 µs/call, repeated calls on the SAME
element, all consistent with a live D-Bus round-trip rather than a cache
hit. Whatever caching libatspi's C library does for the "cache" D-Bus
interface, it evidently doesn't shield the AXObject/AXComponent GI wrapper
calls this code makes — so all three cost the same and get invalidated the
same way. 154 elements at ~1.8ms/element (3 live calls) fully accounts for
the measured ~286-650ms median per render.

ElementCache.get_element_snapshot() below caches (rect, name, role)
together per element, keyed by hash(element), eagerly invalidated by
on_state_changed() (the same event that already triggers a targeted
AXObject.clear_cache() for the OTHER libatspi-side cache) and bounded by
_EXTENTS_TTL_S as a safety net for any change that doesn't correlate with
an AT-SPI event we listen for. AXObject.clear_cache(obj) still flushes
libatspi's own cache for one specific object via
Atspi.Accessible.clear_cache_single() — whatever that cache does cover.

On event (from deckery_events.py):
  children-changed:add    → invalidate parent's libatspi cache, walk the
                            added subtree for relevant elements          O(subtree)
  children-changed:remove → remove event.any_data from the list         O(1)
  window:create           → walk just the new window                    O(window)
  window:destroy          → drop the window entry                       O(1)
  state-changed:*         → clear_cache(obj), re-check eligibility      O(1)

Threading: build_initial() runs on a background thread exactly once. Event
handlers run on the GTK main thread (no background threads after startup,
so no ATSPI_LOCK needed at event-handling time). The _tree_ready flag in
prototype_atspi_overlay.py prevents _render() from reading the cache until
build_initial() has finished and idle_add'd _apply_initial_cache().

Orca code used directly:
  AXObject._find_ancestor_with_role() — walk up tree to nearest window frame
  AXObject.clear_cache(obj)           — targeted libatspi cache invalidation
  AXObject.get_toolkit_name/role/name/state_set/process_id/is_dead/get_parent
  AXComponent.get_rect()
  AXUtilitiesState.is_visible/is_showing/is_focusable/is_enabled
"""

import threading
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

import deckery_filter as filt
import prototype_atspi_scan as scan
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState

# Extents cache TTL — see get_extents() below. Measured (bench_render.py,
# deckery#28): a full render_tree() pass over ~150 elements takes ~300-400ms
# of live get_rect() calls when cold, so a TTL shorter than that would
# expire before a second, closely-spaced render could ever reuse the first
# one's fetches — defeats the cache for exactly the case it targets (a burst
# of renders triggered close together by unrelated events). 500ms comfortably
# covers a render pass while staying two orders of magnitude below the 60s
# full-cache-rebuild safety net (deckery_events.py's FULL_REBUILD_SECONDS),
# which is the actual backstop for a position change that never fires any
# AT-SPI event this cache invalidates on.
_EXTENTS_TTL_S = 0.5

# Serializes all AT-SPI calls during build_initial() against each other.
# Not needed after startup (event handlers run on the main thread, which is
# never concurrent with itself), but build_initial() runs on a background
# thread and libatspi is not safe to call concurrently from multiple threads.
ATSPI_LOCK = threading.RLock()

# Window roles that mark a top-level window boundary when walking up the tree.
_WINDOW_ROLES = frozenset((
    Atspi.Role.FRAME,
    Atspi.Role.DIALOG,
    Atspi.Role.WINDOW,
    Atspi.Role.POPUP_MENU,
    Atspi.Role.NOTIFICATION,
    Atspi.Role.TOOL_TIP,
    Atspi.Role.ALERT,
))


class _WindowMeta:
    """Cached per-window metadata needed for visibility filtering + offset."""
    __slots__ = ("window", "pid", "app_name", "caption", "frame_extents",
                 "is_visible", "is_showing", "is_chromium")

    def __init__(self, window, pid, app_name, caption, frame_extents,
                 is_visible, is_showing, is_chromium):
        self.window = window
        self.pid = pid
        self.app_name = app_name
        self.caption = caption
        self.frame_extents = frame_extents
        self.is_visible = is_visible
        self.is_showing = is_showing
        self.is_chromium = is_chromium


def _extract_window_meta(window, app_name, is_chromium):
    """Extracts _WindowMeta from a window accessible. Caller holds ATSPI_LOCK."""
    pid = AXObject.get_process_id(window)
    caption = AXObject.get_name(window) or ""
    frame_rect = AXComponent.get_rect(window)
    frame_extents = frame_rect if (frame_rect.width or frame_rect.height) else None
    states = AXObject.get_state_set(window)
    is_visible = AXUtilitiesState.is_visible(window, states)
    is_showing = AXUtilitiesState.is_showing(window, states)
    return _WindowMeta(window, pid, app_name, caption, frame_extents,
                       is_visible, is_showing, is_chromium)


def _find_window_ancestor(obj):
    """Walk up the AT-SPI parent chain to find the enclosing window.
    Returns None if no window ancestor is found (e.g. desktop-level object).
    ATSPI_LOCK is reentrant (RLock), so this is safe to call whether or not
    the caller already holds it."""
    with ATSPI_LOCK:
        current = obj
        for _ in range(50):  # cap: avoid infinite loops on malformed trees
            if current is None or AXObject.is_dead(current):
                return None
            role = AXObject.get_role(current)
            if role in _WINDOW_ROLES:
                return current
            current = AXObject.get_parent(current)
        return None


class ElementCache:
    """Orca-style incremental cache of relevant AT-SPI elements.

    Public interface used by deckery_walk.render_tree():
        windows  — iterable of _WindowMeta for all tracked windows
        elements_for(window_hash) — list[Atspi.Accessible] for one window
    """

    def __init__(self):
        # {hash(window): _WindowMeta}
        self._meta = {}
        # {hash(window): list[Atspi.Accessible]} — relevant elements per window
        self._elements = {}
        # {hash(element): (Atspi.Rect, name, role, monotonic_timestamp)} —
        # see get_element_snapshot()
        self._element_data = {}
        # hash(window) of the last window:activate seen, or None. Used by
        # EventRouter's active-window poll (deckery#28) to force-refresh
        # just the focused window's elements on a short timer — see
        # invalidate_window_extents() below for why events alone aren't
        # enough for this.
        self._active_window_hash = None
        # Guards _meta/_elements/_element_data/_active_window_hash against
        # concurrent access (deckery#28): the deckery_backend.py/main.py
        # architecture runs AT-SPI event handling and render_tree() on
        # genuinely different OS threads (confirmed live — event callbacks
        # were observed firing on at least 3 distinct thread ids: the GTK
        # main thread, the dedicated worker thread, and periodic-rebuild's
        # own spawned thread). Before this lock, an event handler mutating
        # self._elements[h] (e.g. .append()/.pop()) while render_tree() was
        # mid-iteration over the same dict/list from another thread was a
        # real, reproduced bug: elements silently disappearing again after
        # a while, the list no longer updating. Public as .lock so
        # deckery_walk.render_tree() can hold it for an entire render pass,
        # not just per individual accessor call — RLock so nested
        # acquisition by the same thread (e.g. on_children_changed_add()
        # calling on_window_create() calling back into a locked method)
        # doesn't deadlock.
        self.lock = threading.RLock()

    # ------------------------------------------------------------------
    # Initial population (background thread)
    # ------------------------------------------------------------------

    def build_initial(self):
        """Full desktop walk, called ONCE from a background thread.
        Populates _meta and _elements. libatspi's D-Bus protocol cache is
        primed as a side-effect of the AXObject/AXComponent calls here —
        subsequent render calls to get_rect()/get_state_set() are served
        from that protocol cache without D-Bus round-trips."""
        with ATSPI_LOCK:
            desktop = Atspi.get_desktop(0)
            meta = {}
            elements = {}
            for i in range(desktop.get_child_count()):
                app = desktop.get_child_at_index(i)
                if app is None:
                    continue
                try:
                    self._walk_app_locked(app, meta, elements)
                except Exception:
                    continue
        # Atomic swap on the main thread via idle_add (caller's responsibility
        # to schedule this result back — see prototype_atspi_overlay.py).
        return meta, elements

    def apply_initial(self, meta, elements):
        """Apply the result of build_initial(). Called after the initial
        walk and after each periodic safety rebuild — see EventRouter."""
        with self.lock:
            self._meta = meta
            self._elements = elements
            # Fresh element set (initial build or the 60s safety rebuild)
            # means every cached entry's key may now reference a
            # stale/replaced object — drop them all rather than risk a hash
            # collision serving a wrong-object's cached data.
            self._element_data = {}

    def _walk_app_locked(self, app, meta, elements):
        """Walk one app's windows. Caller holds ATSPI_LOCK."""
        is_chromium = filt.is_chromium_toolkit(AXObject.get_toolkit_name(app))
        app_name = AXObject.get_name(app) or ""
        unreliable = filt.has_unreliable_visibility_state(app_name)

        for j in range(AXObject.get_child_count(app)):
            window = AXObject.get_child(app, j)
            if window is None:
                continue
            try:
                wm = _extract_window_meta(window, app_name, is_chromium)
            except Exception:
                continue

            # Pre-filter: skip windows that are neither unreliable-visibility
            # apps (plasmashell) nor AT-SPI-visible — same logic as old
            # render_tree()'s is_visible+is_showing check.
            if not unreliable and not (wm.is_visible and wm.is_showing):
                continue
            if unreliable and not filt.frame_has_valid_extents(wm.frame_extents):
                continue

            try:
                elems = scan.walk_elements(window)
            except Exception:
                elems = []

            h = hash(window)
            meta[h] = wm
            elements[h] = elems

    # ------------------------------------------------------------------
    # Incremental event handlers (main thread)
    # ------------------------------------------------------------------

    def on_window_create(self, event):
        """window:create — walk just the new window and add it to the cache."""
        window = event.source
        with ATSPI_LOCK:
            if window is None or AXObject.is_dead(window):
                return
            try:
                app = Atspi.Accessible.get_application(window)
            except Exception:
                app = None
            if app is None:
                return
            is_chromium = filt.is_chromium_toolkit(AXObject.get_toolkit_name(app))
            app_name = AXObject.get_name(app) or ""
            try:
                wm = _extract_window_meta(window, app_name, is_chromium)
                elems = scan.walk_elements(window)
            except Exception:
                return
        h = hash(window)
        with self.lock:
            self._meta[h] = wm
            self._elements[h] = elems
        print(f"[cache] window:create — tracked {h}, {len(elems)} elements")

    def on_window_activate(self, event):
        """window:activate — walk the window only if it's not already cached.
        Catches windows that appear without a prior window:create event.
        Returns True if the window was newly added, False if already tracked
        (caller uses this to suppress an unnecessary render)."""
        window = event.source
        with ATSPI_LOCK:
            if window is None or AXObject.is_dead(window):
                return False
            h = hash(window)
            with self.lock:
                self._active_window_hash = h
                already_tracked = h in self._meta
            if already_tracked:
                return False  # already tracked, nothing to do
            # Not yet in cache — treat like a window:create.
            try:
                app = Atspi.Accessible.get_application(window)
            except Exception:
                app = None
            if app is None:
                return False
            is_chromium = filt.is_chromium_toolkit(AXObject.get_toolkit_name(app))
            app_name = AXObject.get_name(app) or ""
            try:
                wm = _extract_window_meta(window, app_name, is_chromium)
                elems = scan.walk_elements(window)
            except Exception:
                return False
        with self.lock:
            self._meta[h] = wm
            self._elements[h] = elems
        print(f"[cache] window:activate — newly tracked {h}, {len(elems)} elements")
        return True

    def on_window_destroy(self, event):
        """window:destroy — drop the window entry."""
        window = event.source
        h = hash(window)
        with self.lock:
            dropped = h in self._meta
            self._meta.pop(h, None)
            self._elements.pop(h, None)
            if self._active_window_hash == h:
                self._active_window_hash = None
        if dropped:
            print(f"[cache] window:destroy — dropped {h}")

    def on_children_changed_add(self, event):
        """children-changed:add — walk the added subtree and add any relevant
        elements to the containing window's list. event.any_data is the direct
        new child of event.source; it may itself have focusable descendants."""
        child = event.any_data
        with ATSPI_LOCK:
            if child is None or AXObject.is_dead(child):
                return

            # If the added child is itself a window-level object (e.g. a new
            # plasmashell popup appearing), treat it as a window:create.
            role = AXObject.get_role(child)
            if role in _WINDOW_ROLES:
                self.on_window_create(type("_E", (), {"source": child})())
                return

            window = _find_window_ancestor(event.source)
            if window is None:
                return
            h = hash(window)
            with self.lock:
                if h not in self._elements:
                    return  # window not tracked (different desktop etc.)

            # Invalidate parent's libatspi cache so the added child becomes
            # visible to subsequent get_child_count/get_child_at_index calls.
            AXObject.clear_cache(event.source)
            new_elems = scan.walk_elements(child)
        if new_elems:
            with self.lock:
                if h not in self._elements:
                    return  # window got dropped while we were walking
                # Dedup by identity before extending: confirmed live
                # (deckery#28, user report of duplicated boxes after
                # switching desktops away and back) that the same object can
                # reach this method twice — e.g. both a plain
                # "children-changed:add" and its ":system"-suffixed variant
                # routing to the same handler for what's semantically one
                # underlying change (deckery_events.py routes both via
                # startswith()), or an overlapping subtree walk from two
                # separate add events. Extending unconditionally duplicated
                # the object in self._elements[h], drawing the same box
                # twice.
                existing = self._elements[h]
                added = [e for e in new_elems if e not in existing]
                if added:
                    existing.extend(added)
                    print(f"[cache] children-changed:add — +{len(added)} elements in window {h}")

    def on_children_changed_remove(self, event):
        """children-changed:remove — remove the specific element (and any of
        its descendants that were in the list) by object identity."""
        child = event.any_data
        if child is None:
            return

        with ATSPI_LOCK:
            # Window-level removal — treat as window:destroy.
            if not AXObject.is_dead(child):
                role = AXObject.get_role(child)
                if role in _WINDOW_ROLES:
                    self.on_window_destroy(type("_E", (), {"source": child})())
                    return

        # Remove the element (and any sub-elements that are the same object
        # or children of it) from whichever window list contains them.
        removed = 0
        with self.lock:
            for h, elems in list(self._elements.items()):
                before = len(elems)
                # Identity comparison — Atspi.Accessible objects are stable
                # for the lifetime of the remote object (same as Orca's
                # hash(obj) key).
                self._elements[h] = [e for e in elems if e != child]
                removed += before - len(self._elements[h])
            self._element_data.pop(hash(child), None)
        if removed:
            print(f"[cache] children-changed:remove — -{removed} elements")

    def on_state_changed(self, event):
        """state-changed:showing / state-changed:enabled — re-check the
        specific object's eligibility and add or remove it accordingly.
        O(1): only touches this one object, not the whole window."""
        obj = event.source
        with ATSPI_LOCK:
            if obj is None or AXObject.is_dead(obj):
                return

            # Invalidate this object's libatspi protocol cache entry so the
            # next get_state_set() call re-fetches from AT-SPI (targeted,
            # not global).
            AXObject.clear_cache(obj)

            window = _find_window_ancestor(obj)
            if window is None:
                return
            h = hash(window)

            # If this is a window frame that isn't tracked yet, walk it now.
            # Handles popups that appear via state-changed:showing rather
            # than window:create (e.g. some KDE plasmashell popups). Must be
            # checked BEFORE the "h not in self._elements" guard below.
            with self.lock:
                frame_untracked = obj == window and h not in self._meta
            if frame_untracked:
                self.on_window_create(type("_E", (), {"source": window})())
                return

            # now_relevant/states: still under ATSPI_LOCK (may hit D-Bus);
            # self.lock (below) is only for the dict mutation itself.
            now_relevant = scan.is_relevant_element(obj) if obj != window else None
            states = AXObject.get_state_set(window) if obj == window else None

        with self.lock:
            if h not in self._elements:
                return  # window not tracked (different desktop, etc.)

            # If this is a window frame itself, update the meta's is_visible/
            # is_showing (used by render_tree's pre-filter).
            if obj == window:
                wm = self._meta.get(h)
                if wm is not None and states is not None:
                    wm.is_visible = AXUtilitiesState.is_visible(window, states)
                    wm.is_showing = AXUtilitiesState.is_showing(window, states)
                return

            was_in = obj in self._elements[h]
            if now_relevant and not was_in:
                self._elements[h].append(obj)
            elif not now_relevant and was_in:
                self._elements[h].remove(obj)

            # A showing/enabled transition usually means this element's
            # position (and possibly name/role, e.g. a lazily-populated row)
            # just became meaningful — never serve cached data from before
            # the state flip.
            self._element_data.pop(hash(obj), None)

    # ------------------------------------------------------------------
    # Read interface for render_tree()
    # ------------------------------------------------------------------

    @property
    def windows(self):
        """Snapshot list of _WindowMeta for all tracked windows. A list, not
        the live dict view — deckery_walk.render_tree() should hold
        cache.lock for its whole pass anyway, but this stays safe even
        called standalone without the dict-changed-size-during-iteration
        risk a bare .values() view would have under concurrent mutation."""
        with self.lock:
            return list(self._meta.values())

    def elements_for(self, window_hash):
        """List of Atspi.Accessible for one window (by hash(window))."""
        with self.lock:
            return list(self._elements.get(window_hash, []))

    @property
    def active_window_hash(self):
        """hash(window) of the last window:activate seen, or None."""
        with self.lock:
            return self._active_window_hash

    def invalidate_window_extents(self, window_hash):
        """Drops cached (rect, name, role) for every element of one window,
        forcing the next render to fetch fresh data instead of serving the
        TTL-cached snapshot.

        Exists for EventRouter's active-window poll (deckery#28): Qt/QML
        content (confirmed via Qt's own atspiadaptor.cpp and
        qquicklistview.cpp source — deckery session notes) never fires an
        AT-SPI event for a plain position change (QQuickListView calls no
        accessibility update at all when scrolling; Qt's Linux bridge drops
        QAccessible::LocationChanged as a no-op even if something did).
        Event-driven invalidation (on_state_changed, above) structurally
        cannot catch that case — there is no event to invalidate on. A
        periodic poll scoped to just the focused window's already-known
        elements (not a full re-walk, not a full-desktop poll — both
        measured too expensive, see deckery_events.py's FULL_REBUILD_SECONDS
        comment and the desktop-wide vs. single-window element count
        comparison in deckery#28 session notes) is the cheapest way to
        bound how stale a silently-moved element's box can get."""
        with self.lock:
            for element in self._elements.get(window_hash, []):
                self._element_data.pop(hash(element), None)

    def get_element_snapshot(self, element):
        """Cached (rect, name, role) lookup for element.

        All three are live D-Bus round-trips in this environment (measured
        ~450-730 µs/call each, see bench_render.py and the module docstring
        above) — none of them are actually served from libatspi's protocol
        cache the way earlier comments here assumed. Calling all three
        unconditionally for every visible element on every render was the
        dominant cost of render_tree() (up to ~650ms for 154 elements).

        Cached together per element (one entry, one timestamp — they share
        the same staleness lifecycle), eagerly invalidated by
        on_state_changed() (a showing/enabled flip almost always means the
        element's position/name/role is now stale or newly meaningful) and
        bounded by _EXTENTS_TTL_S regardless — a safety net for any change
        (e.g. some scrolled views) that doesn't correlate with any AT-SPI
        event this cache listens on. Worst case, a moving element's box
        lags by at most one TTL window; best case (the common one: a render
        triggered by something other than this element changing — window
        activation, an unrelated popup, a KWin tracker update after a
        desktop switch) it's a dict lookup instead of three D-Bus calls."""
        key = hash(element)
        now = time.monotonic()
        with self.lock:
            cached = self._element_data.get(key)
            if cached is not None and (now - cached[3]) < _EXTENTS_TTL_S:
                return cached[0], cached[1], cached[2]
        # Cache miss/expired — do the live D-Bus calls outside self.lock (our
        # own dict lock) so a slow fetch on one thread doesn't stall every
        # other thread's plain dict access. Still need ATSPI_LOCK around the
        # calls themselves though: libatspi's shared connection is not safe
        # for concurrent multi-threaded use (see ATSPI_LOCK's docstring) —
        # confirmed live (deckery#28) with a libdbus
        # "assertion watch != NULL failed" crash in dbus-watch.c when
        # render_tree() (this path, on the worker thread) and an AT-SPI
        # event handler (on the main thread) hit libatspi at the same time
        # with no lock between them. A harmless race remains possible here:
        # two threads missing on the same key at once both fetch (each under
        # its own ATSPI_LOCK acquisition) and the second self.lock write
        # wins — wastes one duplicate D-Bus call occasionally, never
        # corrupts anything.
        with ATSPI_LOCK:
            rect = AXComponent.get_rect(element)
            name = AXObject.get_name(element) or ""
            role = AXObject.get_role_name(element) or ""
        with self.lock:
            self._element_data[key] = (rect, name, role, now)
        return rect, name, role
