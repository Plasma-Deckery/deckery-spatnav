#!/usr/bin/env python3
"""
deckery_walk.py — render-time transform: cache → screen elements.

As of the Orca-style incremental cache refactor, this module contains only
render_tree() — a pure, zero-AT-SPI-call transform from ElementCache state
to a list of screen-positioned elements.

The expensive AT-SPI work (initial desktop walk, per-event incremental
updates) is now in deckery_cache.py's ElementCache. Render just reads the
cached Atspi.Accessible objects, calls AXComponent.get_rect() on each one
(served from Orca's AXCacheManager — no D-Bus), applies deckery_filter.py's
visibility + offset/scale logic, and returns the result.
"""

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

import deckery_filter as filt
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject


class WalkResult:
    __slots__ = ("elements", "skipped_windows", "fallback_offsets", "elapsed_ms")

    def __init__(self, elements, skipped_windows, fallback_offsets, elapsed_ms):
        self.elements = elements
        self.skipped_windows = skipped_windows
        self.fallback_offsets = fallback_offsets
        self.elapsed_ms = elapsed_ms


def render_tree(tracker, cache):
    """Pure transform: ElementCache + WindowTracker → screen-positioned elements.

    Zero AT-SPI calls: AXComponent.get_rect() is served from Orca's
    AXCacheManager (populated during build_initial() and kept current by
    ElementCache's event handlers). deckery_filter.py's visibility checks
    and offset/scale logic are unchanged."""
    t0 = GLib.get_monotonic_time()
    results = []
    skipped_windows = 0
    fallback_offsets = 0

    for wm in cache.windows:
        # AT-SPI frame-level visibility pre-filter (same logic as before,
        # using is_visible/is_showing captured at walk time in _WindowMeta
        # and updated by ElementCache.on_state_changed when they change).
        if filt.has_unreliable_visibility_state(wm.app_name):
            if not filt.frame_has_valid_extents(wm.frame_extents):
                skipped_windows += 1
                continue
        else:
            if not (wm.is_visible and wm.is_showing):
                skipped_windows += 1
                continue

        if not filt.is_window_visible(tracker, wm.pid, wm.caption):
            skipped_windows += 1
            continue

        # frame_extents may be None if get_rect(window) was called during
        # build_initial() before AT-SPI finished initialising. Lazily fetch
        # it once and cache it in _WindowMeta — subsequent renders use the
        # cached value without any D-Bus calls.
        if wm.frame_extents is None:
            _fr = AXComponent.get_rect(wm.window)
            if _fr and (_fr.width or _fr.height):
                wm.frame_extents = _fr
        frame_width = wm.frame_extents.width if wm.frame_extents is not None else None
        frame_height = wm.frame_extents.height if wm.frame_extents is not None else None

        if filt.is_explicitly_hidden(tracker, wm.pid, frame_width, frame_height):
            skipped_windows += 1
            continue

        offset_x, offset_y, is_fallback = filt.resolve_offset(
            tracker, wm.pid, wm.caption, frame_width, frame_height
        )
        if is_fallback:
            fallback_offsets += 1

        for element in cache.elements_for(hash(wm.window)):
            if AXObject.is_dead(element):
                continue
            rect = AXComponent.get_rect(element)  # Orca cache hit
            name = AXObject.get_name(element) or ""
            role = AXObject.get_role_name(element) or ""
            if rect is None or (not rect.width and not rect.height
                                and not rect.x and not rect.y):
                results.append((0, role, name, None))
                continue
            x, y, w, h = filt.compute_offset_and_scale(
                rect, wm.is_chromium, name, offset_x, offset_y
            )
            rendered = Atspi.Rect()
            rendered.x, rendered.y, rendered.width, rendered.height = x, y, w, h
            results.append((0, role, name, rendered))

    elapsed_ms = (GLib.get_monotonic_time() - t0) / 1000.0
    return WalkResult(results, skipped_windows, fallback_offsets, elapsed_ms)
