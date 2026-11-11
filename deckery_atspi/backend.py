"""Ties scene, visibility, matching, reading and composing together.

Single-threaded: everything runs on the default GLib main context.

Two kinds of updates (BACKEND_V2_DESIGN.md §7):
- scene change (KWin push) → re-match + compose from cached raw elements;
  AT-SPI is only asked about windows that are visible and not cached yet.
- content change (AT-SPI event from an app) → that app's visible windows
  are re-read after a short debounce, then composed.

Two cases where content moves without any AT-SPI event (found with the
test desktop's freshness check, tools/testdesk/ctl.py stale_targets):
- window resized → Qt re-lays out (Dolphin icon grid) silently: a window
  whose size differs from when it was read gets re-read (debounced, so an
  interactive resize doesn't re-read on every frame);
- small scroll inside a QML ListView (no delegate recycled, so no
  children-changed): the active window's targets are re-sampled every
  ACTIVE_POLL_MS and the window is re-read if any of them moved."""

import dataclasses
import os
import time

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from .atspi_frames import POPUP_ROLES, element_id, frame_info, list_frames
from .compose import compose_window
from .matching import match_windows
from .placement import placement_for
from .reader import live_rect, read_window
from .scene import LAYER_OVERLAY
from .visibility import visible_regions

# Trailing debounce: an update runs once events have been quiet for
# CONTENT_DEBOUNCE_MS, but never later than CONTENT_MAX_DELAY_MS after the
# first one — plasmashell fires hundreds of state-changed events while a
# flyout opens (test desktop 2026-11-11), each re-read costing up to 600 ms.
CONTENT_DEBOUNCE_MS = 150
CONTENT_MAX_DELAY_MS = 600
# One follow-up re-read this long after an event-driven one: QML animates
# layout changes without bounds-changed events, so the first re-read can
# catch the panel mid-animation (pager added → task buttons sliding right,
# test desktop 2026-11-11) and nothing would ever correct it.
SETTLE_REREAD_MS = 700
ACTIVE_POLL_MS = 300
# Upper bound on live calls per poll (~0.5 ms each).
ACTIVE_POLL_MAX_ELEMENTS = 60
# Frames are re-listed on window:create/destroy; this bounds how often an
# unmatched visible window can trigger an extra re-list.
FRAME_RELIST_MIN_INTERVAL_S = 2.0

_CONTENT_EVENTS = (
    "object:children-changed",
    "object:state-changed:showing",
    "object:state-changed:visible",
    "object:bounds-changed",
    "object:property-change:accessible-name",
)
_WINDOW_EVENTS = ("window:create", "window:destroy")


class Backend:
    def __init__(self, log=print, on_elements=None):
        self._log = log
        self._on_elements = on_elements
        self.scene = None
        self.frames = None
        self._frames_listed_at = 0.0
        self.matches = {}
        self.elements = []
        # window_id → (frame_id, (width, height), [RawElement])
        self._raw = {}
        self._dirty_bus_names = set()
        self._dirty_windows = set()
        # Open popups (context menus, submenus) by element id: Qt doesn't
        # list them under their application, so they are only known from
        # their own state-changed:showing events.
        self._popups = {}
        self._content_timer = 0
        self._first_pending_at = 0.0
        self._settle_timer = 0
        self._settle_bus_names = set()
        self._listener = None
        # KWin windows a frame re-list was already tried for: a window KWin
        # reports for the first time gets one immediate re-list (a new
        # flyout/popup's frame is usually there already), after that only
        # the rate-limited retry.
        self._relisted_for = set()

    # ---- inputs ----

    def start_events(self):
        self._listener = Atspi.EventListener.new(self._on_atspi_event)
        for event in _CONTENT_EVENTS + _WINDOW_EVENTS:
            self._listener.register(event)
        GLib.timeout_add(ACTIVE_POLL_MS, self._poll_active_window)

    def _poll_active_window(self):
        scene = self.scene
        if scene is None or self._content_timer:
            return GLib.SOURCE_CONTINUE
        cached = self._raw.get(scene.active_window_id)
        if cached is None:
            return GLib.SOURCE_CONTINUE
        for element in [e for e in cached[2] if not e.inferred][:ACTIVE_POLL_MAX_ELEMENTS]:
            r = live_rect(element.accessible)
            if abs(r.x - element.rect.x) > 0.5 or abs(r.y - element.rect.y) > 0.5 \
                    or abs(r.w - element.rect.w) > 0.5 or abs(r.h - element.rect.h) > 0.5:
                self._dirty_windows.add(scene.active_window_id)
                self._schedule_update()
                break
        return GLib.SOURCE_CONTINUE

    def on_scene(self, scene):
        self.scene = scene
        self._update(reread_bus_names=())

    def _on_atspi_event(self, event):
        try:
            app = event.source.app if event.source else None
            bus_name = app.bus_name if app else None
        except Exception:
            return
        if event.type.startswith("window:"):
            self.frames = None  # re-list on next update
        elif event.type == "object:state-changed:showing" and self._track_popup(event):
            pass
        elif event.type.startswith("object:property-change:accessible-name"):
            # Name changes never change structure, and most come from
            # labels (panel clock with seconds, CPU %) that are never
            # targets — confirmed live: ~2/s from plasmashell alone, each
            # used to trigger a full ~600 ms panel re-read. Patch the name
            # of a known target in place, ignore everything else.
            if self._patch_name(element_id(event.source), event.source):
                self._schedule_update()
            return
        if bus_name:
            self._dirty_bus_names.add(bus_name)
            if os.environ.get("DECKERY_DEBUG_EVENTS"):
                self._log(f"[event] {event.type} {bus_name}")
        self._schedule_update()

    def _track_popup(self, event):
        try:
            if event.source.get_role_name() not in POPUP_ROLES:
                return False
            eid = element_id(event.source)
            if event.detail1:
                self._popups[eid] = event.source
            else:
                self._popups.pop(eid, None)
        except Exception:
            return False
        return True

    def _all_frames(self):
        """Application frames + currently open popups. Popups that died or
        stopped showing without an event are dropped here."""
        frames = list(self.frames)
        known = {f.id for f in frames}
        for eid, acc in list(self._popups.items()):
            try:
                info = frame_info(acc)
            except Exception:
                del self._popups[eid]
                continue
            if not info.showing:
                del self._popups[eid]
            elif eid not in known:
                frames.append(info)
        return frames

    def _schedule_update(self):
        now = time.monotonic()
        if not self._content_timer:
            self._first_pending_at = now
        else:
            GLib.source_remove(self._content_timer)
        remaining_ms = CONTENT_MAX_DELAY_MS - 1000 * (now - self._first_pending_at)
        delay = max(0, min(CONTENT_DEBOUNCE_MS, int(remaining_ms)))
        self._content_timer = GLib.timeout_add(delay, self._flush_content)

    def _patch_name(self, eid, source):
        for window_id, (frame_id, size, raw) in self._raw.items():
            for i, element in enumerate(raw):
                if element.id == eid:
                    try:
                        name = source.get_name() or ""
                    except Exception:
                        return False
                    raw[i] = dataclasses.replace(element, name=name)
                    return True
        return False

    def _flush_content(self):
        self._content_timer = 0
        dirty, self._dirty_bus_names = self._dirty_bus_names, set()
        windows, self._dirty_windows = self._dirty_windows, set()
        self._update(reread_bus_names=dirty, reread_windows=windows)
        if dirty:
            self._settle_bus_names |= dirty
            if self._settle_timer:
                GLib.source_remove(self._settle_timer)
            self._settle_timer = GLib.timeout_add(SETTLE_REREAD_MS, self._settle_reread)
        return GLib.SOURCE_REMOVE

    def _settle_reread(self):
        self._settle_timer = 0
        dirty, self._settle_bus_names = self._settle_bus_names, set()
        # Straight to _update: going through _flush_content would schedule
        # yet another settle pass.
        self._update(reread_bus_names=dirty)
        return GLib.SOURCE_REMOVE

    # ---- core ----

    def _relist_frames(self):
        t0 = time.monotonic()
        self.frames = list_frames()
        self._frames_listed_at = time.monotonic()
        self._log(f"[frames] {len(self.frames)} AT-SPI frames in {1000 * (self._frames_listed_at - t0):.0f} ms")

    def _match(self, scene):
        candidates = [w for w in scene.windows if w.layer < LAYER_OVERLAY]
        self.frames_all = self._all_frames()
        return match_windows(candidates, self.frames_all)

    def _update(self, reread_bus_names, reread_windows=()):
        scene = self.scene
        if scene is None:
            return
        t0 = time.monotonic()
        if self.frames is None:
            self._relist_frames()
        regions = visible_regions(scene)
        self.matches, unmatched = self._match(scene)
        missing = {wid for wid in regions if wid in unmatched}
        if missing - self._relisted_for or (
                missing and time.monotonic() - self._frames_listed_at > FRAME_RELIST_MIN_INTERVAL_S):
            self._relisted_for |= missing
            self._relist_frames()
            self.matches, unmatched = self._match(scene)

        by_id = {w.id: w for w in scene.windows}
        elements = []
        reads = []
        for window_id, region in regions.items():
            window = by_id[window_id]
            match = self.matches.get(window_id)
            if match is None:
                continue
            frame = self.frames_all[match.frame_index]
            cached = self._raw.get(window_id)
            bus_name = frame.id.split("/", 1)[0]
            size = (round(window.buffer.w), round(window.buffer.h))
            reason = ("new" if cached is None else "frame" if cached[0] != frame.id
                      else "event" if bus_name in reread_bus_names
                      else "poll/size" if window_id in reread_windows else None)
            if reason:
                if "popupWindow" in window.flags and (cached is None or cached[0] != frame.id):
                    self._log(f"[popup] kwin {window.frame} ← {frame.role} {frame.name!r} "
                              f"{frame.width}x{frame.height} ({match.rule})")
                raw, stats = read_window(frame.accessible, frame.toolkit)
                self._raw[window_id] = (frame.id, size, raw)
                reads.append(f"{window.resource_class}:{len(raw)}el/{stats.nodes}n/{stats.ms:.0f}ms({reason})")
            elif cached[1] != size:
                self._dirty_windows.add(window_id)
                self._schedule_update()
            raw = self._raw[window_id][2]
            elements.extend(compose_window(raw, window_id, placement_for(scene, window, frame.toolkit, frame.toolkit_version), region))

        for window_id in list(self._raw):
            if window_id not in by_id:
                del self._raw[window_id]

        self.elements = elements
        if reads:
            self._log(f"[update] {len(elements)} targets, {1000 * (time.monotonic() - t0):.0f} ms, "
                      f"read {' '.join(reads)}")
        if self._on_elements:
            self._on_elements(elements)

    def unmatched_report(self):
        """For logging: visible windows that could not be matched."""
        if self.scene is None:
            return []
        regions = visible_regions(self.scene)
        _, unmatched = self._match(self.scene)
        by_id = {w.id: w for w in self.scene.windows}
        return [(by_id[w].resource_class, by_id[w].caption, unmatched[w]) for w in regions if w in unmatched]
