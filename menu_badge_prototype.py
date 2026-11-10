#!/usr/bin/env python3
"""
menu_badge_prototype.py — minimal prototype for MENU_HIJACK_DESIGN.md.

Draws a small square badge to the left of every item in whatever menu/popup
menu/submenu is currently open, using CoordType.SCREEN directly.

Offset correction, deliberately narrow (MENU_HIJACK_DESIGN.md §20 — read
that section before changing this again): a menu's raw CoordType.SCREEN
position is trusted as-is UNLESS it's exactly (0,0), in which case we
substitute the currently ACTIVE window's own (reliably tracked)
on-screen offset instead. This covers every confirmed "no real position of
its own" case — the Global Menu/appmenu dropdown (app='kded6': several
same-sized ~1x1px anchor placeholder windows all captioned literally
'kded6', none individually identifiable) and the system tray flyout
(hosted by kwin_wayland itself, which has no window of its own at all) —
because both only ever show the active window's own menu anyway.

A prior version of this file instead reused deckery_filter.resolve_offset()
— the main production pipeline's pid/caption/size-keyed offset lookup —
unconditionally for every menu, to also fix a narrower case this exact-(0,0)
gate misses (Kirigami-style "hamburger"/Control-button popups, e.g. System
Settings'/Dolphin's, which report a non-zero but still-wrong window-relative
position). That generalization broke three OTHER previously-working cases
in turn (ambiguous ties, wrong per-pid matches, a correction that doubled an
already-correct position — full postmortem in MENU_HIJACK_DESIGN.md §20.2).
Reverted back to this narrower version, the last one confirmed stable across
menu open/submenu open/close/reopen and window moves. The Kirigami-popup
case is a known, intentionally unfixed gap, not an oversight — see §20.4
before trying to close it again.

Tracks each open menu by object identity (hash(accessible)) so submenus
opening/closing are handled independently, per MENU_HIJACK_DESIGN.md §8.

Run: distrobox enter deckery -- python3 menu_badge_prototype.py
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

_LAYER_SHELL_PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if _LAYER_SHELL_PRELOAD not in os.environ.get("LD_PRELOAD", "").split(":"):
    existing = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = (
        f"{_LAYER_SHELL_PRELOAD}:{existing}" if existing else _LAYER_SHELL_PRELOAD
    )
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(sys.executable, [sys.executable] + sys.argv)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Atspi", "2.0")

from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk, Gio, Atspi
import cairo

from deckery_tracker import WindowTracker, _ts

MENU_ROLES = (Atspi.Role.MENU, Atspi.Role.POPUP_MENU)
ITEM_ROLES = (Atspi.Role.MENU_ITEM, Atspi.Role.CHECK_MENU_ITEM, Atspi.Role.RADIO_MENU_ITEM)

BADGE_PADDING = 4

# How close to the screen origin a menu's own reported CoordType.SCREEN
# position has to be before we treat it as "not a real position, use the
# active window's offset instead." Confirmed live (deckery#28, MENU_HIJACK_
# DESIGN.md §20): BOTH the Global Menu/appmenu dropdown (app='kded6') AND
# the system tray flyout (hosted directly by kwin_wayland itself — no
# window of its own at all) always report exactly (0,0) regardless of their
# real position — a compositor-level "no real window" signal, not specific
# to kded6. Ordinary menus that already position correctly can legitimately
# have small-but-nonzero coordinates near a screen edge, so this is
# deliberately an exact-zero check, not a fuzzy "near the corner" one, to
# avoid wrongly correcting an already-correct menu.
_BROKEN_POSITION_THRESHOLD = 0

# Auto-screenshot on every menu OPEN (deckery#28, at the user's request —
# makes live position-correction testing faster than manually screenshotting
# after each open). spectacle isn't installed inside the deckery distrobox
# container this script runs in, so it's invoked on the HOST via
# distrobox-host-exec (same pattern already used for qdbus-qt6 elsewhere in
# this project) — meaning the -o path must be the HOST-visible path, not the
# container one. On this ostree/bootc system /home is really /var/home, but
# which spelling os.path.abspath(__file__) resolves to depends on this
# process's CWD at launch (confirmed live, deckery#28: the distrobox exec
# environment sets --workdir=/var/home/..., so it's ALREADY the /var form
# here, not /home) — blindly prepending "/var" then doubled it to
# "/var/var/home/...", a path that doesn't exist, which is why every
# screenshot silently failed. Strip + re-add instead of guessing which form
# we started with.
_SCREENSHOT_DIR_CONTAINER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools", "menu_screenshots"
)
_SCREENSHOT_DIR_HOST = "/var" + _SCREENSHOT_DIR_CONTAINER.removeprefix("/var")
# "Space-saving": keep only the most recent N shots instead of letting this
# grow unbounded across a long testing session.
_SCREENSHOT_KEEP_LAST = 80
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


# Max attempts per screenshot. Confirmed live (deckery#28): the FIRST
# `distrobox-host-exec spectacle ...` call after a gap exits 0 with empty
# stdout/stderr but writes NO file at all — a one-time connection-warm-up
# race in host-spawn (the channel distrobox-host-exec forwards commands to
# the host through), not a spectacle problem; calls right after it succeed
# normally. Retrying beats trying to fix the race itself (upstream, not
# ours to fix) — cheap since failures are instant (no hung process, just a
# silent no-op).
_SCREENSHOT_MAX_ATTEMPTS = 3
_SCREENSHOT_RETRY_DELAY_S = 0.4


def _capture_menu_screenshot(label):
    """One full-screen PNG via the host's spectacle, named so chronological
    ls/sort matches event order. Runs in its own thread (subprocess.run is
    synchronous, with retries — see _SCREENSHOT_MAX_ATTEMPTS — so this must
    not run on the GTK main thread). Never lets an exception escape the
    thread — screenshotting must not be able to take down the badge
    overlay."""
    def _worker():
        try:
            os.makedirs(_SCREENSHOT_DIR_CONTAINER, exist_ok=True)
            existing = sorted(
                f for f in os.listdir(_SCREENSHOT_DIR_CONTAINER) if f.endswith(".png")
            )
            for stale in existing[: max(0, len(existing) - _SCREENSHOT_KEEP_LAST + 1)]:
                try:
                    os.remove(os.path.join(_SCREENSHOT_DIR_CONTAINER, stale))
                except OSError:
                    pass
            safe_label = _SAFE_NAME_RE.sub("_", label)[:60]
            filename = f"{_ts().replace(':', '-')}_{safe_label}.png"
            host_path = os.path.join(_SCREENSHOT_DIR_HOST, filename)
            container_path = os.path.join(_SCREENSHOT_DIR_CONTAINER, filename)
            for attempt in range(1, _SCREENSHOT_MAX_ATTEMPTS + 1):
                subprocess.run(
                    ["distrobox-host-exec", "spectacle", "-b", "-n", "-f", "-o", host_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
                if os.path.exists(container_path) and os.path.getsize(container_path) > 0:
                    print(f"[{_ts()}] [menu-badge] [screenshot] {filename} "
                          f"(attempt {attempt}/{_SCREENSHOT_MAX_ATTEMPTS})")
                    return
                time.sleep(_SCREENSHOT_RETRY_DELAY_S)
            print(f"[{_ts()}] [menu-badge] [screenshot] FAILED after "
                  f"{_SCREENSHOT_MAX_ATTEMPTS} attempts: {filename}")
        except Exception as e:
            print(f"[{_ts()}] [menu-badge] [screenshot] failed (non-fatal): {e}")

    threading.Thread(target=_worker, daemon=True).start()


class MenuTracker:
    """hash(menu_accessible) -> list of (item_accessible) currently in it."""

    def __init__(self, on_change, window_tracker):
        self._on_change = on_change
        self._window_tracker = window_tracker
        self._open_menus = {}  # {hash(menu): menu_accessible}
        # {hash(menu): (pid, name)} — remembered per tracked menu so
        # collect_items() can log/label without re-querying AT-SPI for
        # identity (its own extents ARE re-queried every render, since
        # those can legitimately change — deckery#28).
        self._menu_info = {}
        # {pid} — keyed by PID, not by individual menu, because a submenu's
        # own CoordType.SCREEN position is NOT itself (0,0) once broken —
        # it's already relative to the same broken origin as its parent menu
        # (confirmed live, deckery#28: top-level Global Menu position fixed
        # correctly via the (0,0) check below, but its own submenus still
        # landed wrong, needing the SAME correction, not a fresh
        # (0,0)-triggered one). Once a pid is confirmed broken (because SOME
        # menu belonging to it reported exactly (0,0)), the active window's
        # offset applies to every menu/submenu sharing that pid, checked
        # first before even looking at the new menu's own position.
        self._broken_pids = set()

    def start(self):
        listener = Atspi.EventListener.new(self._on_event)
        listener.register("object:state-changed:showing")
        print(f"[{_ts()}] [menu-badge] listening for object:state-changed:showing")

    def _offset_for(self, pid, rect):
        """Returns (off_x, off_y) to add to this menu's (and its items')
        raw CoordType.SCREEN coordinates — see module docstring and
        MENU_HIJACK_DESIGN.md §20.1 for why this is deliberately narrow."""
        if pid in self._broken_pids:
            return self._active_offset()
        if rect.x > _BROKEN_POSITION_THRESHOLD or rect.y > _BROKEN_POSITION_THRESHOLD:
            return (0, 0)
        offset = self._active_offset()
        if offset != (0, 0):
            self._broken_pids.add(pid)
        return offset

    def _active_offset(self):
        active_pid = self._window_tracker.active_pid
        if active_pid is None:
            return (0, 0)
        return self._window_tracker.client_offset_by_pid.get(active_pid, (0, 0))

    def _on_event(self, event):
        try:
            role = event.source.get_role()
        except Exception:
            return
        if role not in MENU_ROLES:
            return

        h = hash(event.source)
        is_showing = bool(event.detail1)
        if is_showing:
            if h not in self._open_menus:
                self._open_menus[h] = event.source
                try:
                    pid = Atspi.Accessible.get_process_id(event.source)
                except Exception:
                    pid = None
                try:
                    name = event.source.get_name() or ""
                except Exception:
                    name = ""
                self._menu_info[h] = (pid, name)
                # name alone is often useless for telling menus apart — many
                # report '' or a generic app name shared by several distinct
                # popups of that pid (deckery#28, see the user's request to
                # tell menus apart programmatically). first_item + the
                # active window's own caption are much more specific: e.g.
                # name='' first_item='New Tab' active_caption='Konsole'
                # unambiguously identifies Konsole's File menu even when
                # name itself carries no information.
                first_item = ""
                try:
                    for i in range(min(event.source.get_child_count(), 5)):
                        child = event.source.get_child_at_index(i)
                        if child is not None and child.get_role() in ITEM_ROLES:
                            first_item = child.get_name() or ""
                            if first_item:
                                break
                except Exception:
                    pass
                active_pid = self._window_tracker.active_pid
                active_caption = ""
                if active_pid is not None:
                    captions = self._window_tracker.all_captions_by_pid.get(active_pid, set())
                    non_empty = [c for c in captions if c]
                    if non_empty:
                        active_caption = non_empty[0]
                print(f"[{_ts()}] [menu-badge] menu OPEN: {h} name={name!r} "
                      f"first_item={first_item!r} active_caption={active_caption!r} "
                      f"pid={pid} active_pid={active_pid} (role={role.value_name})")
                self._on_change()
                # Delay lets refresh()'s redraw actually land before the
                # shot is taken — queue_draw() is async (see BadgeOverlay).
                label = f"open_{name or active_caption or 'unnamed'}_{first_item}"
                GLib.timeout_add(250, lambda: (_capture_menu_screenshot(label), False)[1])
        else:
            if h in self._open_menus:
                del self._open_menus[h]
                self._menu_info.pop(h, None)
                print(f"[{_ts()}] [menu-badge] menu CLOSED: {h}")
                self._on_change()

    def collect_items(self):
        """Returns list of (item_rect, item_name) for every item in every
        currently-tracked open menu, using CoordType.SCREEN directly."""
        results = []
        for h, menu in list(self._open_menus.items()):
            try:
                state_set = menu.get_state_set()
                # Defensive close-detection (deckery#28 MENU_HIJACK_DESIGN.md):
                # confirmed live that some popups (plasmashell/Kirigami- and
                # DBusMenu/appmenu-hosted ones — the same "unreliable
                # visibility state" class documented for plasmashell in
                # REQUIREMENTS.md §13.1) never fire state-changed:showing
                # with detail1=0 on close. Relying solely on that event left
                # badges permanently stuck for the tray menu, a Dolphin
                # context menu, and the Global Menu dropdown alike. Since
                # we're already touching this object every refresh anyway,
                # double-check its CURRENT live state here rather than
                # trusting only the (sometimes-missing) event.
                if state_set.contains(Atspi.StateType.DEFUNCT):
                    del self._open_menus[h]
                    self._menu_info.pop(h, None)
                    continue
                if not state_set.contains(Atspi.StateType.SHOWING):
                    print(f"[{_ts()}] [menu-badge] menu {h} no longer SHOWING on live "
                          f"check (missed its close event) — dropping")
                    del self._open_menus[h]
                    self._menu_info.pop(h, None)
                    continue
                n = menu.get_child_count()
                menu_rect = Atspi.Component.get_extents(menu, Atspi.CoordType.SCREEN)
            except Exception:
                continue
            pid, name = self._menu_info.get(h, (None, ""))
            off_x, off_y = self._offset_for(pid, menu_rect)
            print(f"[{_ts()}] [menu-badge] collect_items: menu={h} pid={pid} name={name!r} "
                  f"active_pid={self._window_tracker.active_pid} "
                  f"raw_pos=({menu_rect.x},{menu_rect.y}) offset=({off_x},{off_y}) n_children={n}")
            for i in range(n):
                try:
                    child = menu.get_child_at_index(i)
                    if child is None:
                        continue
                    if child.get_role() not in ITEM_ROLES:
                        continue
                    rect = Atspi.Component.get_extents(child, Atspi.CoordType.SCREEN)
                    if rect.width <= 0 or rect.height <= 0:
                        continue
                    if off_x or off_y:
                        rect.x += off_x
                        rect.y += off_y
                    name = child.get_name() or ""
                    results.append((rect, name))
                except Exception:
                    continue
        return results


class ATSPIEventSink:
    """Additive, read-only consumer of MenuTracker's existing output — does
    not touch menu detection/correction logic at all (that stays exclusively
    owned by this file's MenuTracker, see MAKIMA_INTEGRATION.md). Pushes a
    generic Event(typ, payload) to Makima on every menu-state change, using
    the exact push-via-D-Bus-method-call pattern Makima already established
    for window focus (kde.rs's org.makima.watcher/WindowActivated) — see
    MAKIMA_INTEGRATION.md §4 for why this shape (one generic Event, not a
    bespoke method per feature) and why D-Bus (established precedent, no new
    transport).

    Makima does not implement a receiver for org.makima.atspi yet — that
    work is explicitly out of scope right now (separate, ongoing elsewhere).
    Every call here is therefore expected to fail with
    GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown; this class only
    proves OUR side builds and fires the right event at the right time,
    which can be checked by comparing the "[atspi-ipc]" lines below against
    this file's existing "[menu-badge] refresh:"/"menu OPEN"/"menu CLOSED"
    lines — same count, same moments, no drift."""

    BUS_NAME = "org.makima.atspi"
    OBJECT_PATH = "/ATSPI"
    INTERFACE = "org.makima.atspi"

    def __init__(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            print(f"[{_ts()}] [atspi-ipc] could not connect to session bus: {e}")
            self._bus = None

    def emit(self, typ, payload):
        payload_json = json.dumps(payload)
        print(f"[{_ts()}] [atspi-ipc] Event(typ={typ!r}, payload={payload_json})")
        if self._bus is None:
            return
        try:
            self._bus.call_sync(
                self.BUS_NAME, self.OBJECT_PATH, self.INTERFACE, "Event",
                GLib.Variant("(ss)", (typ, payload_json)),
                None, Gio.DBusCallFlags.NONE, 500, None,
            )
            print(f"[{_ts()}] [atspi-ipc] delivered")
        except GLib.Error as e:
            # Expected for now — no Makima-side receiver yet (see class
            # docstring). Logged, not raised: this must never take down the
            # badge overlay just because Makima isn't listening.
            print(f"[{_ts()}] [atspi-ipc] not delivered (no receiver yet): {e}")

    def emit_menu_state(self, tracker):
        """typ='atspi.menu_state': the full current item list across every
        open menu/submenu, straight from tracker.collect_items() — the same
        data BadgeOverlay.refresh() already draws from, unmodified."""
        items = tracker.collect_items()
        payload = [
            {"name": name, "x": rect.x, "y": rect.y, "w": rect.width, "h": rect.height}
            for rect, name in items
        ]
        self.emit("atspi.menu_state", payload)


class BadgeOverlay(Gtk.Window):
    def __init__(self, tracker):
        super().__init__()
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(self, edge, True)
        self.set_decorated(False)
        self.add_css_class("menu-badge-transparent")

        self.tracker = tracker
        self.items = []

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._draw, None)
        self.set_child(self.drawing_area)

        css = Gtk.CssProvider()
        css.load_from_data(b"window.menu-badge-transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def refresh(self):
        self.items = self.tracker.collect_items()
        print(f"[{_ts()}] [menu-badge] refresh: {len(self.items)} item(s) to badge")
        self.drawing_area.queue_draw()

    def _draw(self, area, cr, width, height, data):
        if self.get_surface():
            self.get_surface().set_input_region(cairo.Region())
        # Explicit clear-to-transparent: cairo's default OVER operator means
        # drawing nothing here does NOT erase a previous frame's boxes, so a
        # menu closing (self.items -> []) left the last frame's boxes stuck
        # on screen at their old position/offset instead of disappearing —
        # confirmed live (user report of badges stuck "in the wrong place"
        # right after switching menus).
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()
        # GTK calls this draw_func asynchronously whenever it gets around to
        # repainting after queue_draw() — logged separately from refresh()
        # (which only schedules the draw) so timing-sensitive bugs can be
        # diagnosed from the log instead of guessed at from screenshots.
        print(f"[{_ts()}] [menu-badge] _draw: painting {len(self.items)} item(s) "
              f"at {[(round(r.x), round(r.y)) for r, _ in self.items]}")
        for rect, name in self.items:
            side = max(rect.height - 2 * BADGE_PADDING, 10)
            # Menus near the left screen edge (e.g. kded6's small window
            # menu at x=4) push a left-side badge into negative X — off
            # screen. Flip to the right of the item when there's no room
            # on the left (confirmed live: user report of badges "landing
            # outside the screen").
            bx = rect.x - side - BADGE_PADDING
            if bx < 0:
                bx = rect.x + rect.width + BADGE_PADDING
            by = rect.y + (rect.height - side) / 2
            cr.set_source_rgba(0.95, 0.75, 0.10, 0.85)
            cr.rectangle(bx, by, side, side)
            cr.fill()
            cr.set_source_rgba(0, 0, 0, 1)
            cr.rectangle(bx, by, side, side)
            cr.set_line_width(1)
            cr.stroke()
            # also outline the real item's rect for visual sanity-checking
            cr.set_source_rgba(0.95, 0.75, 0.10, 0.9)
            cr.set_line_width(2)
            cr.rectangle(rect.x, rect.y, rect.width, rect.height)
            cr.stroke()


def main():
    app = Gtk.Application(application_id="dev.deckery.menubadge")

    def on_activate(app):
        # Reused as-is from the main production pipeline (deckery_tracker.py)
        # — only for its pid->offset map here, not full desktop-visibility
        # filtering. Must not run at the same time as main.py: both would
        # try to own org.deckery.WindowTracker, and whichever loses falls
        # back to no data (the exact D-Bus collision fixed earlier this
        # session for main.py/cursor_nav_prototype.py).
        window_tracker = WindowTracker(on_update=None)

        overlay = BadgeOverlay(None)
        atspi_sink = ATSPIEventSink()

        # Additive fan-out: every existing refresh trigger below also pushes
        # the same data to Makima via ATSPIEventSink, on top of (not instead
        # of) the badge redraw — see ATSPIEventSink's docstring. `tracker` is
        # read here only once this actually runs (menu events, the earliest
        # of which fires after tracker.start() below), so the forward
        # reference to the not-yet-assigned name is fine.
        def on_menu_change():
            overlay.refresh()
            atspi_sink.emit_menu_state(tracker)

        tracker = MenuTracker(on_change=on_menu_change, window_tracker=window_tracker)
        overlay.tracker = tracker

        # Without this, a moved window's corrected offset only reaches the
        # screen via the 1s safety-net poll below or the next menu open/close
        # — visible live as "badge draws at the stale position, then jumps a
        # second later" (deckery#28). Redraw the instant KWin's geometry push
        # arrives instead, same thread/context as everything else here so a
        # direct call (no idle_add dance) is safe.
        window_tracker._on_update = on_menu_change
        window_tracker.start()
        tracker.start()
        overlay.set_application(app)
        overlay.present()
        # Safety-net poll (deckery#28): if a menu's close event never fires
        # (see collect_items()'s live-SHOWING recheck), refresh() otherwise
        # never gets called again for it — nothing re-triggers the recheck.
        # Without another, unrelated menu happening to open/close elsewhere,
        # a stale badge would stay forever. 1s is generous (this is a
        # prototype, not the production render path) but bounds the
        # worst-case staleness of a missed close event.
        GLib.timeout_add_seconds(1, lambda: (on_menu_change(), True)[1])

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
