#!/usr/bin/env python3
"""
deckery_walk.py — AT-SPI desktop tree walking + render-time filtering
(deckery#28).

As of the event-driven rewrite, this module is split into two halves that
run at very different frequencies:

  - walk_app() / build_full_tree(): the only place that makes AT-SPI calls.
    Walks one Application (or the whole desktop) and returns RAW,
    window-relative element data — no visibility filtering, no on-screen
    offset applied yet. Called once at startup (build_full_tree()) and then
    incrementally, one app at a time, by deckery_events.TreeCache in
    response to real AT-SPI events (children-changed, state-changed:showing,
    etc.) instead of being re-run wholesale on a timer. This is the
    expensive part (each element costs its own D-Bus round-trip), which is
    exactly why it no longer runs unconditionally every REFRESH_SECONDS.

  - render_tree(): pure data transform, ZERO AT-SPI calls. Takes the cached
    tree from TreeCache plus the WindowTracker's live desktop-membership
    state and produces the final on-screen element list: visibility
    filtering (deckery_filter.is_window_visible) + offset/scale
    (deckery_filter.compute_offset_and_scale). Cheap enough to call on every
    tracker update (desktop switch, window move) without touching AT-SPI at
    all — the previous version re-walked the entire AT-SPI tree for this,
    which is what made desktop switches expensive for no reason (the tree
    itself hadn't changed, only which part of it should be drawn).

    Bug fixed here (deckery#28, post-event-driven-rewrite live test):
    render_tree() used to call AXObject.get_state_set(win.window) +
    AXUtilitiesState.is_visible/is_showing PER WINDOW on every single call —
    a real AT-SPI/D-Bus round-trip despite the "ZERO AT-SPI calls" promise
    above. Confirmed live: render times crept from ~10ms to 800-1600ms over
    a ~15min run and the overlay process eventually died silently (no
    traceback) — consistent with a blocking D-Bus call on the GTK main
    thread (render_tree() is called synchronously from
    OverlayWindow._render()). Fixed by moving the VISIBLE/SHOWING state
    query into walk_app() (WindowEntry.is_visible/is_showing, captured once
    per raw walk) instead of re-querying live on every render. Staying
    correct after the raw walk relies on deckery_events.EVENT_TYPES already
    including object:state-changed:showing/enabled, which triggers a fresh
    walk_app() re-walk for the owning app whenever these actually change.

do_walk() still exists as a convenience wrapper (build_full_tree +
render_tree) for one-shot/fallback use — see
prototype_atspi_overlay.py's _initial_fallback_refresh and TreeCache's
periodic full-rebuild safety net.

Threading note (deckery#28, found via coredumpctl after repeated live
crashes): libatspi's underlying D-Bus/libffi marshalling is NOT safe to call
concurrently from multiple threads — confirmed by four separate SIGSEGV
crashes (coredumpctl: PIDs 323455/326087/332389/334631, all "Stack trace ...
libatspi.so ... libffi.so") produced by deckery_events.TreeCache's design of
spawning a new daemon thread per AT-SPI event to run walk_app(), which could
overlap with each other and with the main-thread's own
Atspi.Accessible.get_application() call in _on_event. ATSPI_LOCK below
serializes every AT-SPI-calling entry point (walk_app, build_full_tree, and
the get_application lookup in deckery_events.py) across all threads so at
most one is ever talking to libatspi at a time — the background threads
still exist (so a slow walk can't freeze GTK's redraw loop), they just can't
run concurrently WITH EACH OTHER or with the main thread anymore. Orca
itself sidesteps this entirely by never spawning threads for AT-SPI access
in the first place (single-threaded, always on the main loop) — worth
revisiting if this lock turns out to reintroduce noticeable stalls.
"""

import threading

from gi.repository import Atspi, GLib

import prototype_atspi_scan as scan
import deckery_filter as filt
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState

# See module docstring's "Threading note". RLock (not Lock) because
# build_full_tree() acquires this and then calls walk_app(), which acquires
# it again on the same thread — a plain Lock would deadlock on that reentry.
ATSPI_LOCK = threading.RLock()


class WalkResult:
    __slots__ = ("elements", "skipped_windows", "fallback_offsets", "elapsed_ms")

    def __init__(self, elements, skipped_windows, fallback_offsets, elapsed_ms):
        self.elements = elements
        self.skipped_windows = skipped_windows
        self.fallback_offsets = fallback_offsets
        self.elapsed_ms = elapsed_ms


class WindowEntry:
    """One AT-SPI top-level window's raw (un-offset, unfiltered) data.

    is_visible/is_showing are captured HERE, at walk time, rather than
    queried live in render_tree() — see deckery_walk.py's module docstring
    ("Bug fixed here") for why: querying them live turned render_tree() into
    a hidden per-window AT-SPI/D-Bus round-trip, breaking its "zero AT-SPI
    calls" contract and causing render times to creep from ~10ms to
    800-1600ms until the overlay process died silently."""

    __slots__ = ("window", "pid", "caption", "frame_extents", "raw_elements", "is_visible", "is_showing")

    def __init__(self, window, pid, caption, frame_extents, raw_elements, is_visible, is_showing):
        self.window = window
        self.pid = pid
        self.caption = caption
        self.frame_extents = frame_extents
        self.raw_elements = raw_elements  # list of (depth, role, name, extents)
        self.is_visible = is_visible
        self.is_showing = is_showing


class AppEntry:
    """One AT-SPI Application's raw data: its toolkit + all its windows."""

    __slots__ = ("app", "app_name", "is_chromium_app", "windows")

    def __init__(self, app, app_name, is_chromium_app, windows):
        self.app = app
        self.app_name = app_name
        self.is_chromium_app = is_chromium_app
        self.windows = windows  # list of WindowEntry


def walk_app(app):
    """Walks one Application's windows and returns an AppEntry with RAW
    (window-relative, unfiltered) element data. The only AT-SPI-calling
    function callers should need for incremental updates — re-walking one
    app after an event is dramatically cheaper than walking the whole
    desktop, since it's normally exactly one app whose tree actually
    changed.

    Holds ATSPI_LOCK for its whole body — see module docstring's "Threading
    note" for why every AT-SPI-calling entry point needs to serialize
    against every other one, across all threads."""
    with ATSPI_LOCK:
        n_windows = AXObject.get_child_count(app)

        # Toolkit name is exposed per-Application, not per-element, so it's
        # looked up once per app and reused for all its windows/elements. Note:
        # AXObject.get_toolkit_name() lowercases (unlike the raw
        # Atspi.Accessible.get_toolkit_name() this replaced) — see
        # filt.is_chromium_toolkit()'s matching lowercase comparison.
        is_chromium_app = filt.is_chromium_toolkit(AXObject.get_toolkit_name(app))
        app_name = AXObject.get_name(app) or ""

        windows = []
        for j in range(n_windows):
            window = AXObject.get_child(app, j)
            if window is None:
                continue

            frame_rect = AXComponent.get_rect(window)
            frame_extents = frame_rect if (frame_rect.width or frame_rect.height) else None

            pid = AXObject.get_process_id(window)
            caption = AXObject.get_name(window) or ""

            # Captured once per raw walk, not re-queried live in render_tree()
            # (see WindowEntry's docstring for why that used to be a bug) — a
            # window's own frame-level VISIBLE/SHOWING states, used by
            # render_tree() as the "necessary but not sufficient" pre-filter
            # before the real KWin virtual-desktop-membership check.
            window_states = AXObject.get_state_set(window)
            is_visible = AXUtilitiesState.is_visible(window, window_states)
            is_showing = AXUtilitiesState.is_showing(window, window_states)

            try:
                # Note: some apps (Electron, apps without AT-SPI support) have no
                # focusable elements in their tree. scan.walk() will return an
                # empty list, which is expected — the window frame itself is
                # focusable but children aren't exposed to AT-SPI (see
                # ATSPI_LIMITATIONS.md).
                raw_elements = scan.walk(window)
            except Exception:
                raw_elements = []

            windows.append(
                WindowEntry(window, pid, caption, frame_extents, raw_elements, is_visible, is_showing)
            )

        return AppEntry(app, app_name, is_chromium_app, windows)


def build_full_tree():
    """Walks every app on the AT-SPI desktop once. Returns a dict keyed by
    hash(app) -> AppEntry (vendor/orca_ax's ax_cache_manager uses the same
    hash(accessible)-as-key convention — Atspi.Accessible objects compare/
    hash consistently for the same remote object across separate calls).
    This is the expensive, full-desktop-walk operation — call it once at
    startup and thereafter only as TreeCache's periodic safety-net rebuild,
    not on every tick.

    Holds ATSPI_LOCK for its whole body (an RLock, so the per-app walk_app()
    calls inside re-acquiring it on the same thread is safe) — see module
    docstring's "Threading note"."""
    with ATSPI_LOCK:
        desktop = Atspi.get_desktop(0)
        tree = {}
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            try:
                tree[hash(app)] = walk_app(app)
            except Exception:
                continue
        return tree


def render_tree(tracker, tree):
    """Pure data transform: applies visibility filtering + on-screen offset/
    scale to the cached `tree` (a dict as returned by build_full_tree(),
    kept live by deckery_events.TreeCache) using the WindowTracker's current
    state. Makes NO AT-SPI calls — safe and cheap to call on every tracker
    update (desktop switch, window move/resize) without re-walking
    anything."""
    t0 = GLib.get_monotonic_time()
    results = []
    skipped_windows = 0
    fallback_offsets = 0

    unreliable_visibility_apps = {
        app_entry: filt.has_unreliable_visibility_state(app_entry.app_name)
        for app_entry in tree.values()
    }

    for app_entry in tree.values():
        unreliable_visibility = unreliable_visibility_apps[app_entry]

        for win in app_entry.windows:
            if unreliable_visibility:
                # See filt.UNRELIABLE_VISIBILITY_APPS: this app's own
                # VISIBLE/SHOWING states are known to be stuck one way or
                # the other for its popups/flyouts, so use "has this frame
                # ever been laid out with a real size" instead.
                if not filt.frame_has_valid_extents(win.frame_extents):
                    skipped_windows += 1
                    continue
            else:
                # Frame-level VISIBLE+SHOWING excludes offscreen helper
                # frames but, per KWin, does NOT reflect virtual-desktop
                # membership (a window on another desktop still reports
                # VISIBLE=True/SHOWING=True). So it's necessary but not
                # sufficient — the real desktop-membership check below (via
                # the KWin window-tracker) is what actually restricts this
                # to the current desktop.
                if not (win.is_visible and win.is_showing):
                    skipped_windows += 1
                    continue

            if not filt.is_window_visible(tracker, win.pid, win.caption):
                skipped_windows += 1
                continue

            frame_width = win.frame_extents.width if win.frame_extents is not None else None
            frame_height = win.frame_extents.height if win.frame_extents is not None else None

            # Skip windows that KWin explicitly reported as hidden — auto-hide
            # panels currently tucked away. Must come after is_window_visible()
            # (so we've already confirmed the pid/desktop is relevant) and
            # after frame_width/height are computed (needed for the size-key
            # lookup, since plasmashell panels have empty captions).
            if filt.is_explicitly_hidden(tracker, win.pid, frame_width, frame_height):
                skipped_windows += 1
                continue

            offset_x, offset_y, is_fallback = filt.resolve_offset(
                tracker, win.pid, win.caption, frame_width, frame_height
            )
            if is_fallback:
                fallback_offsets += 1

            for depth, role, name, extents in win.raw_elements:
                if extents is None:
                    results.append((depth, role, name, None))
                    continue
                x, y, width, height = filt.compute_offset_and_scale(
                    extents, app_entry.is_chromium_app, name, offset_x, offset_y
                )
                rendered = Atspi.Rect()
                rendered.x, rendered.y, rendered.width, rendered.height = x, y, width, height
                results.append((depth, role, name, rendered))

    elapsed_ms = (GLib.get_monotonic_time() - t0) / 1000.0
    return WalkResult(results, skipped_windows, fallback_offsets, elapsed_ms)


def do_walk(tracker):
    """Convenience one-shot wrapper (build_full_tree + render_tree) for
    callers that don't need a persistent TreeCache — the initial fallback
    refresh and ad-hoc/manual testing. The live overlay path
    (prototype_atspi_overlay.py) uses deckery_events.TreeCache instead so
    the expensive build_full_tree() half doesn't re-run on every tracker
    update."""
    tree = build_full_tree()
    return render_tree(tracker, tree)
