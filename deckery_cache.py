#!/usr/bin/env python3
"""
deckery_cache.py — Orca-style incremental element cache.

Replaces the old AppEntry/WindowEntry/TreeCache design. Instead of
re-walking an entire app on every AT-SPI event (O(n*m) D-Bus calls),
we maintain a flat dict of which Atspi.Accessible objects are currently
relevant (focusable+showing+enabled), keyed by their containing window.

Caching: AXComponent.get_rect() and AXObject.get_state_set() go through
libatspi's own D-Bus protocol cache (not Orca's higher-level AXCacheManager,
which caches derived values like object attributes and dead-object status).
After build_initial() primes libatspi's cache via the initial walk, render
calls to AXComponent.get_rect(element) are served from that protocol cache
without D-Bus round-trips. AXObject.clear_cache(obj) flushes libatspi's
cache for one specific object via Atspi.Accessible.clear_cache_single() —
the right scalpel for state-changed/children-changed invalidation instead of
nuking everything.

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

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

import deckery_filter as filt
import prototype_atspi_scan as scan
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState

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
    Returns None if no window ancestor is found (e.g. desktop-level object)."""
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
        """Apply the result of build_initial() — must be called on main thread."""
        self._meta = meta
        self._elements = elements

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
        self._meta[h] = wm
        self._elements[h] = elems
        print(f"[cache] window:create — tracked {h}, {len(elems)} elements")

    def on_window_activate(self, event):
        """window:activate — walk the window only if it's not already cached.
        Catches windows that appear without a prior window:create event.
        Returns True if the window was newly added, False if already tracked
        (caller uses this to suppress an unnecessary render)."""
        window = event.source
        if window is None or AXObject.is_dead(window):
            return False
        h = hash(window)
        if h in self._meta:
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
        self._meta[h] = wm
        self._elements[h] = elems
        print(f"[cache] window:activate — newly tracked {h}, {len(elems)} elements")
        return True

    def on_window_destroy(self, event):
        """window:destroy — drop the window entry."""
        window = event.source
        h = hash(window)
        dropped = h in self._meta
        self._meta.pop(h, None)
        self._elements.pop(h, None)
        if dropped:
            print(f"[cache] window:destroy — dropped {h}")

    def on_children_changed_add(self, event):
        """children-changed:add — walk the added subtree and add any relevant
        elements to the containing window's list. event.any_data is the direct
        new child of event.source; it may itself have focusable descendants."""
        child = event.any_data
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
        if h not in self._elements:
            return  # window not tracked (different desktop etc.)

        # Invalidate parent's libatspi cache so the added child becomes
        # visible to subsequent get_child_count/get_child_at_index calls.
        AXObject.clear_cache(event.source)
        new_elems = scan.walk_elements(child)
        if new_elems:
            self._elements[h].extend(new_elems)
            print(f"[cache] children-changed:add — +{len(new_elems)} elements in window {h}")

    def on_children_changed_remove(self, event):
        """children-changed:remove — remove the specific element (and any of
        its descendants that were in the list) by object identity."""
        child = event.any_data
        if child is None:
            return

        # Window-level removal — treat as window:destroy.
        if not AXObject.is_dead(child):
            role = AXObject.get_role(child)
            if role in _WINDOW_ROLES:
                self.on_window_destroy(type("_E", (), {"source": child})())
                return

        # Remove the element (and any sub-elements that are the same object
        # or children of it) from whichever window list contains them.
        removed = 0
        for h, elems in self._elements.items():
            before = len(elems)
            # Identity comparison — Atspi.Accessible objects are stable for
            # the lifetime of the remote object (same as Orca's hash(obj) key).
            self._elements[h] = [e for e in elems if e != child]
            removed += before - len(self._elements[h])
        if removed:
            print(f"[cache] children-changed:remove — -{removed} elements")

    def on_state_changed(self, event):
        """state-changed:showing / state-changed:enabled — re-check the
        specific object's eligibility and add or remove it accordingly.
        O(1): only touches this one object, not the whole window."""
        obj = event.source
        if obj is None or AXObject.is_dead(obj):
            return

        # Invalidate this object's libatspi protocol cache entry so the next
        # get_state_set() call re-fetches from AT-SPI (targeted, not global).
        AXObject.clear_cache(obj)

        window = _find_window_ancestor(obj)
        if window is None:
            return
        h = hash(window)

        # If this is a window frame that isn't tracked yet, walk it now.
        # Handles popups that appear via state-changed:showing rather than
        # window:create (e.g. some KDE plasmashell popups). Must be checked
        # BEFORE the "h not in self._elements" guard below.
        if obj == window and h not in self._meta:
            self.on_window_create(type("_E", (), {"source": window})())
            return

        if h not in self._elements:
            return  # window not tracked (different desktop, etc.)

        # If this is a window frame itself, update the meta's is_visible/
        # is_showing (used by render_tree's pre-filter).
        if obj == window:
            wm = self._meta.get(h)
            if wm is not None:
                states = AXObject.get_state_set(window)
                wm.is_visible = AXUtilitiesState.is_visible(window, states)
                wm.is_showing = AXUtilitiesState.is_showing(window, states)
            return

        now_relevant = scan.is_relevant_element(obj)
        was_in = obj in self._elements[h]
        if now_relevant and not was_in:
            self._elements[h].append(obj)
        elif not now_relevant and was_in:
            self._elements[h].remove(obj)

    # ------------------------------------------------------------------
    # Read interface for render_tree()
    # ------------------------------------------------------------------

    @property
    def windows(self):
        """Iterable of _WindowMeta for all tracked windows."""
        return self._meta.values()

    def elements_for(self, window_hash):
        """List of Atspi.Accessible for one window (by hash(window))."""
        return self._elements.get(window_hash, [])
