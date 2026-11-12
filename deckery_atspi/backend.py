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

from vendor.orca_ax.ax_object import AXObject

from .atspi_bus import shared_bus
from .atspi_frames import POPUP_ROLES, element_id, frame_info, list_frames
from .compose import compose_window
from .geometry import Rect
from .matching import Match, match_windows, pick_popover
from .placement import placement_for
from .reader import live_rects, read_window
from .scene import LAYER_OVERLAY
from .visibility import visible_regions

# Trailing debounce: an update runs once events have been quiet for
# CONTENT_DEBOUNCE_MS, but never later than CONTENT_MAX_DELAY_MS after the
# first one — plasmashell fires hundreds of state-changed events while a
# flyout opens (test desktop 2026-11-11), each re-read costing up to 600 ms.
CONTENT_DEBOUNCE_MS = 150
CONTENT_MAX_DELAY_MS = 600
# One follow-up re-read this long after an event- or poll-driven one: QML animates
# layout changes without bounds-changed events, so the first re-read can
# catch the panel mid-animation (pager added → task buttons sliding right,
# test desktop 2026-11-11) and nothing would ever correct it.
SETTLE_REREAD_MS = 700
# Source of children-changed on the desktop root when apps come and go.
REGISTRY_BUS_NAME = "org.a11y.atspi.Registry"
# A frame without children when read. Chromium builds its tree only after
# a11y gets switched on, a moment after the window appears, and sends no
# event when done (test desktop 2026-11-12: 1 node at window:create, 255 a
# second later). Re-read with these delays until something is there.
EMPTY_FRAME_REREAD_MS = (300, 600, 1200, 2400, 4800)
ACTIVE_POLL_MS = 300
# Upper bound on live calls per poll (asked all at once: ~0.1 ms each,
# one at a time it was ~0.5 ms each).
ACTIVE_POLL_MAX_ELEMENTS = 60
# Frames are re-listed on window:create/destroy; this bounds how often an
# unmatched visible window can trigger an extra re-list.
FRAME_RELIST_MIN_INTERVAL_S = 2.0
# How often an unmatched popup window may be looked for again among its
# main window's children (backend._match_embedded_popups).
EMBEDDED_POPUP_RETRY_S = 0.2
# Firefox rounds its popup size in CSS pixels, KWin's is logical at
# fractional scale: 198x243 vs 199.09x245.45 at scale 1.1 (test desktop
# 2026-11-12). Position and size both have to fit.
EMBEDDED_POPUP_TOLERANCE = 4.0
# An app that missed the frame list's ping or timed out while being read
# is not read again until it answers this ping (sent without blocking,
# again PROBE_RETRY_MS after each timeout). Real desktop 2026-11-12,
# heavy load: plasmashell hung for ~2.5 min; its windows were still read
# on every update, each read waiting for three 3 s timeouts — 45 s per
# update, the panel outlines stood still meanwhile.
PROBE_TIMEOUT_MS = 2000
PROBE_RETRY_MS = 1000
APP_ROOT_PATH = "/org/a11y/atspi/accessible/root"

_CONTENT_EVENTS = (
    "object:children-changed",
    "object:state-changed:showing",
    "object:state-changed:visible",
    # Chromium sends nothing else when a <select> opens (test desktop
    # 2026-11-12): its list is read into the main window and shown in
    # its popup window (_match_popovers).
    "object:state-changed:expanded",
    "object:bounds-changed",
    "object:property-change:accessible-name",
)
_WINDOW_EVENTS = ("window:create", "window:destroy")


def _bus_name(eid):
    return eid.split("/", 1)[0]


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
        self._probes = {}  # window_id -> ReadStats.probes of its last read
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
        self._settle_windows = set()
        self._listener = None
        # KWin windows a frame re-list was already tried for: a window KWin
        # reports for the first time gets one immediate re-list (a new
        # flyout/popup's frame is usually there already), after that only
        # the rate-limited retry.
        self._relisted_for = set()
        self._empty_rereads = {}  # window_id -> re-reads of an empty frame so far
        # bus name -> since; not read until it answers again (_probe_app)
        self._unresponsive_apps = {}
        self._last_frames = []  # the last list_frames result, kept when self.frames is reset
        self._embedded_popups = {}  # popup window_id -> FrameInfo (embedded)
        self._embedded_tried = {}  # popup window_id -> monotonic time of the last search
        self._popovers_logged = set()

    # ---- inputs ----

    def start_events(self):
        self._listener = Atspi.EventListener.new(self._on_atspi_event)
        for event in _CONTENT_EVENTS + _WINDOW_EVENTS:
            self._listener.register(event)
        GLib.timeout_add(ACTIVE_POLL_MS, self._poll_active_window)

    def _poll_active_window(self):
        scene = self.scene
        if scene is None or self._content_timer:
            if os.environ.get("DECKERY_DEBUG_POLL") and scene is not None:
                self._log("[poll] skipped: content timer pending")
            return GLib.SOURCE_CONTINUE
        cached = self._raw.get(scene.active_window_id)
        if cached is None or _bus_name(cached[0]) in self._unresponsive_apps:
            return GLib.SOURCE_CONTINUE
        # Scroll probes first: a page scrolled past all its targets moves
        # none of the cached ones (test desktop 2026-11-12, Chromium sends no
        # scroll events at all).
        samples = (self._probes.get(scene.active_window_id, []) +
                   [e for e in cached[2] if not e.inferred])[:ACTIVE_POLL_MAX_ELEMENTS]
        live = live_rects(samples)
        if live is None:
            # Every 300 ms another timeout otherwise.
            self._mark_unresponsive(_bus_name(cached[0]))
            return GLib.SOURCE_CONTINUE
        if os.environ.get("DECKERY_DEBUG_POLL"):
            self._log(f"[poll] {scene.active_window_id[:9]} {len(samples)} samples, first "
                      f"{[(s.rect.y, r.y) for s, r in list(zip(samples, live))[:3]]}")
        for sample, r in zip(samples, live):
            rect = sample.rect
            if abs(r.x - rect.x) > 0.5 or abs(r.y - rect.y) > 0.5 \
                    or abs(r.w - rect.w) > 0.5 or abs(r.h - rect.h) > 0.5:
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
        elif bus_name == REGISTRY_BUS_NAME and event.type.startswith("object:children-changed"):
            # An application (un)registered. A killed app never sends
            # window:destroy, and its stale frame would still be matched by
            # caption (test desktop 2026-11-12: restarted Chromium).
            self.frames = None
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
            if _bus_name(eid) in self._unresponsive_apps:
                continue  # frame_info would block on it
            # frame_info's libatspi calls can dispatch AT-SPI events, and a
            # showing=False one already pops eid (real desktop 2026-11-12:
            # KeyError here left stale menu outlines behind).
            try:
                info = frame_info(acc)
            except Exception:
                self._popups.pop(eid, None)
                continue
            if not info.showing:
                self._popups.pop(eid, None)
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
        if dirty or windows:
            # Poll-triggered re-reads too: Chromium updates positions first
            # and SHOWING a moment later when a list scrolls; a fast read in
            # between keeps scrolled-out rows as targets, and the poll only
            # compares rects (test desktop 2026-11-12, batched reader).
            self._settle_bus_names |= dirty
            self._settle_windows |= windows
            if self._settle_timer:
                GLib.source_remove(self._settle_timer)
            self._settle_timer = GLib.timeout_add(SETTLE_REREAD_MS, self._settle_reread)
        return GLib.SOURCE_REMOVE

    def _settle_reread(self):
        self._settle_timer = 0
        dirty, self._settle_bus_names = self._settle_bus_names, set()
        windows, self._settle_windows = self._settle_windows, set()
        # Straight to _update: going through _flush_content would schedule
        # yet another settle pass.
        self._update(reread_bus_names=dirty, reread_windows=windows)
        return GLib.SOURCE_REMOVE

    def _reread_empty_later(self, window_id):
        tries = self._empty_rereads.get(window_id, 0)
        if tries >= len(EMPTY_FRAME_REREAD_MS):
            return
        self._empty_rereads[window_id] = tries + 1

        def reread():
            self._dirty_windows.add(window_id)
            self._schedule_update()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(EMPTY_FRAME_REREAD_MS[tries], reread)

    # ---- core ----

    def _relist_frames(self):
        t0 = time.monotonic()
        silent_before = set(self._unresponsive_apps)
        self.frames = list_frames(self._unresponsive_apps, self._last_frames)
        self._last_frames = self.frames
        self._frames_listed_at = time.monotonic()
        newly_silent = set(self._unresponsive_apps) - silent_before
        self._log(f"[frames] {len(self.frames)} AT-SPI frames in {1000 * (self._frames_listed_at - t0):.0f} ms"
                  + (f", not answering: {' '.join(sorted(newly_silent))}" if newly_silent else ""))
        for bus_name in newly_silent:
            self._probe_app(bus_name)

    def _mark_unresponsive(self, bus_name):
        if bus_name in self._unresponsive_apps:
            return
        self._unresponsive_apps[bus_name] = time.monotonic()
        self._log(f"[frames] {bus_name} not answering, not read until it does")
        self._probe_app(bus_name)

    def _probe_app(self, bus_name):
        bus = shared_bus()
        if bus is not None:
            bus.ping(bus_name, APP_ROOT_PATH, PROBE_TIMEOUT_MS,
                     lambda answered: self._on_probe_reply(bus_name, answered))

    def _on_probe_reply(self, bus_name, answered):
        if bus_name not in self._unresponsive_apps:
            return
        if not answered:
            def retry():
                if bus_name in self._unresponsive_apps:
                    self._probe_app(bus_name)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(PROBE_RETRY_MS, retry)
            return
        silent_s = time.monotonic() - self._unresponsive_apps.pop(bus_name)
        self._log(f"[frames] {bus_name} answers again after {silent_s:.1f} s")
        # Its frame list and content may have changed meanwhile.
        self.frames = None
        self._dirty_bus_names.add(bus_name)
        self._schedule_update()

    def _match(self, scene):
        candidates = [w for w in scene.windows if w.layer < LAYER_OVERLAY]
        self.frames_all = self._all_frames()
        matches, unmatched = match_windows(candidates, self.frames_all)
        self._match_embedded_popups(scene, candidates, matches, unmatched)
        return matches, unmatched

    def _match_embedded_popups(self, scene, windows, matches, unmatched):
        """Popup windows whose AT-SPI side is no frame of its own but a
        child of the main window's frame, in the main window's
        coordinates. Test desktop 2026-11-12, Firefox 152: app menu
        ("panel") and context menus ("menu") are children of the browser
        frame; KWin shows each as its own popup window, which matched no
        frame, while the main window's copy of its items lay under that
        popup and was occluded — the menu had no targets at all."""
        for window in windows:
            if window.id not in unmatched or "popupWindow" not in window.flags:
                continue
            info = self._embedded_popups.get(window.id)
            if info is None:
                now = time.monotonic()
                if now - self._embedded_tried.get(window.id, 0.0) < EMBEDDED_POPUP_RETRY_S:
                    continue
                self._embedded_tried[window.id] = now
                info = self._find_embedded_popup(scene, window, windows, matches)
                if info is None:
                    continue
                self._embedded_popups[window.id] = info
            self.frames_all.append(info)
            matches[window.id] = Match(len(self.frames_all) - 1, "embedded-popup")
            del unmatched[window.id]

    def _match_popovers(self, scene, regions, by_id):
        """Popup windows without content of their own, shown for a popup
        read into their main window (RawElement.popup): GTK4 popovers
        (children of the expanded menu button / drop-down) and Chromium's
        open <select> list (its own frame is empty), test desktop
        2026-11-12. A popup is the one whose elements all lie inside the
        KWin popup window. Returns {popup window id: (main window id,
        popup id)}."""
        out = {}
        for window_id in regions:
            popup = by_id[window_id]
            if "popupWindow" not in popup.flags or self._raw.get(window_id, (0, 0, ()))[2]:
                continue
            area = popup.client or popup.frame
            mains = sorted((w for w in scene.windows if w.pid == popup.pid and w.id in self.matches
                            and w.id in self._raw and "popupWindow" not in w.flags),
                           key=lambda w: -(w.frame.area if w.frame else 0))
            for main in mains:
                frame = self.frames_all[self.matches[main.id].frame_index]
                place = placement_for(scene, main, frame.toolkit, frame.toolkit_version)
                groups = {}
                for e in self._raw[main.id][2]:
                    if e.popup:
                        groups.setdefault(e.popup, []).append(place.to_screen(e.rect, e.web))
                for _, taken in out.values():
                    groups.pop(taken, None)
                popup_id = pick_popover(area, groups, EMBEDDED_POPUP_TOLERANCE)
                if popup_id:
                    out[window_id] = (main.id, popup_id)
                    break
        return out

    def _find_embedded_popup(self, scene, popup, windows, matches):
        """The SHOWING child of a same-pid main window's frame that lies
        exactly where KWin shows the popup."""
        mains = sorted((w for w in windows if w.pid == popup.pid and w.id in matches
                        and "popupWindow" not in w.flags),
                       key=lambda w: -(w.frame.area if w.frame else 0))
        target = popup.client
        for main in mains:
            frame = self.frames_all[matches[main.id].frame_index]
            if frame.embedded or _bus_name(frame.id) in self._unresponsive_apps:
                continue
            place = placement_for(scene, main, frame.toolkit, frame.toolkit_version)
            for child in AXObject.iter_children(frame.accessible):
                try:
                    ext = child.get_extents(Atspi.CoordType.WINDOW)
                except Exception:
                    continue
                if ext.width <= 0 or ext.height <= 0:
                    continue
                r = place.to_screen(Rect(ext.x, ext.y, ext.width, ext.height))
                if max(abs(r.x - target.x), abs(r.y - target.y),
                       abs(r.w - target.w), abs(r.h - target.h)) > EMBEDDED_POPUP_TOLERANCE:
                    continue
                try:
                    info = frame_info(child)
                except Exception:
                    continue
                if info.showing:
                    return dataclasses.replace(info, embedded=True, pid=frame.pid,
                                               toolkit=frame.toolkit,
                                               toolkit_version=frame.toolkit_version)
        return None

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
        to_compose = []
        for window_id, region in regions.items():
            window = by_id[window_id]
            match = self.matches.get(window_id)
            if match is None:
                continue
            frame = self.frames_all[match.frame_index]
            cached = self._raw.get(window_id)
            bus_name = _bus_name(frame.id)
            silent = bus_name in self._unresponsive_apps
            size = (round(window.buffer.w), round(window.buffer.h))
            reason = ("new" if cached is None else "frame" if cached[0] != frame.id
                      else "event" if bus_name in reread_bus_names
                      else "poll/size" if window_id in reread_windows else None)
            if reason and silent:
                # Not read while it hangs (_probe_app); what it showed
                # last stays, KWin still moves, hides and covers it.
                if cached is None or cached[0] != frame.id:
                    continue
                reason = None
            if reason:
                if "popupWindow" in window.flags and (cached is None or cached[0] != frame.id):
                    self._log(f"[popup] kwin {window.frame} ← {frame.role} {frame.name!r} "
                              f"{frame.width}x{frame.height} ({match.rule})")
                if reason == "poll/size":
                    # Not triggered by an event, so libatspi's cached
                    # children/states of this window may be outdated: it
                    # only updates them from events, and Chromium sends
                    # none for a tree it builds or a page it scrolls (test
                    # desktop 2026-11-12: a frame cached as childless stayed
                    # so while a fresh process saw 255 nodes).
                    # (The batched reader doesn't use libatspi's cache, but
                    # frame_info and DECKERY_READER=sync do.)
                    frame.accessible.clear_cache()
                raw, stats = read_window(frame.accessible, frame.toolkit, rebase=frame.embedded)
                if stats.timed_out:
                    # Hung: keep what it showed last, as for a known silent app.
                    self._mark_unresponsive(bus_name)
                    reads.append(f"{window.resource_class}:timeout/{stats.ms:.0f}ms({reason})")
                    if cached is None or cached[0] != frame.id:
                        continue
                else:
                    self._raw[window_id] = (frame.id, size, raw)
                    self._probes[window_id] = stats.probes
                    if stats.nodes <= 1:
                        self._log(f"[empty] {window.resource_class} ← {frame.id} {frame.role} {frame.name!r} "
                                  f"{frame.width}x{frame.height} ({match.rule})")
                        self._reread_empty_later(window_id)
                    else:
                        self._empty_rereads.pop(window_id, None)
                    reads.append(f"{window.resource_class}:{len(raw)}el/{stats.nodes}n/{stats.ms:.0f}ms{stats.note}({reason})")
            elif cached[1] != size and not silent:
                self._dirty_windows.add(window_id)
                self._schedule_update()
            to_compose.append((window_id, window, frame, region))

        popovers = self._match_popovers(scene, regions, by_id)
        shown_elsewhere = {}  # main window id -> popup ids composed into their popup window
        for main_id, popup_id in popovers.values():
            shown_elsewhere.setdefault(main_id, set()).add(popup_id)
        for window_id, window, frame, region in to_compose:
            raw = self._raw[window_id][2]
            if window_id in shown_elsewhere:
                raw = [e for e in raw if e.popup not in shown_elsewhere[window_id]]
            elements.extend(compose_window(raw, window_id, placement_for(scene, window, frame.toolkit, frame.toolkit_version), region))
        for window_id, (main_id, popup_id) in popovers.items():
            if window_id not in self._popovers_logged:
                self._log(f"[popup] kwin {by_id[window_id].frame} ← popup {popup_id} of {main_id[:9]} (popover)")
            main = by_id[main_id]
            frame = self.frames_all[self.matches[main_id].frame_index]
            raw = [e for e in self._raw[main_id][2] if e.popup == popup_id]
            # placed as in the main window: the popup is in its coordinates
            elements.extend(compose_window(raw, window_id, placement_for(scene, main, frame.toolkit, frame.toolkit_version), regions[window_id]))
        self._popovers_logged = set(popovers)

        for window_id in list(self._raw):
            if window_id not in by_id:
                del self._raw[window_id]
                self._probes.pop(window_id, None)
                self._empty_rereads.pop(window_id, None)
        for window_id in list(self._embedded_popups) + list(self._embedded_tried):
            if window_id not in by_id:
                self._embedded_popups.pop(window_id, None)
                self._embedded_tried.pop(window_id, None)

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
