#!/usr/bin/env python3
"""
menu_badge_prototype.py — minimal prototype for MENU_HIJACK_DESIGN.md.

Draws a small square badge to the left of every item in whatever menu/popup
menu/submenu is currently open, using CoordType.SCREEN directly — this
turned out to be correct for most menus (Plasma tray, Dolphin context menu)
but NOT for the Global Menu/appmenu dropdown (app='kded6'), which always
reports (0,0) regardless of its real panel-button position (confirmed live,
MENU_HIJACK_DESIGN.md §10/§17). For that case, this reuses
deckery_tracker.WindowTracker — the same KWin-provided pid->offset map the
main production pipeline (deckery_filter.py) already uses for regular
windows — to correct it, triggered only when a menu's own reported position
is exactly (0,0) so already-correct menus aren't double-corrected. No
button/combo assignment yet — just "can we detect + position correctly",
per the user's request for a first small prototype.

Tracks each open menu by object identity (hash(accessible)) so submenus
opening/closing are handled independently, per MENU_HIJACK_DESIGN.md §8.

Run: distrobox enter deckery -- python3 menu_badge_prototype.py
"""

import os
import sys

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

from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk, Atspi
import cairo

from deckery_tracker import WindowTracker

MENU_ROLES = (Atspi.Role.MENU, Atspi.Role.POPUP_MENU)
ITEM_ROLES = (Atspi.Role.MENU_ITEM, Atspi.Role.CHECK_MENU_ITEM, Atspi.Role.RADIO_MENU_ITEM)

BADGE_PADDING = 4

# How close to the screen origin a menu's own reported CoordType.SCREEN
# position has to be before we treat it as "probably broken, not a real
# position" and try a pid-based offset correction instead (deckery#28,
# MENU_HIJACK_DESIGN.md §10/§17). Confirmed live: the Global Menu
# (DBusMenu/appmenu dropdown, app='kded6') reports exactly (0,0) regardless
# of where its panel button actually is — but ordinary menus (Plasma tray,
# Dolphin context menu) that already position correctly can legitimately
# have small-but-nonzero coordinates near a screen edge, so this is
# deliberately an exact-zero check, not a fuzzy "near the corner" one, to
# avoid wrongly double-correcting an already-correct menu.
_BROKEN_POSITION_THRESHOLD = 0


class MenuTracker:
    """hash(menu_accessible) -> list of (item_accessible) currently in it."""

    def __init__(self, on_change, window_tracker):
        self._on_change = on_change
        self._window_tracker = window_tracker
        self._open_menus = {}  # {hash(menu): menu_accessible}
        # {hash(menu): pid} — remembered per tracked menu so collect_items()
        # can look up its correction (if any) without re-querying AT-SPI.
        self._menu_pid = {}
        # {pid} — keyed by PID, not by individual menu, because a submenu's
        # own CoordType.SCREEN position is NOT itself (0,0) once broken —
        # it's already relative to the same broken origin as its parent menu
        # (confirmed live, deckery#28: top-level Global Menu position fixed
        # correctly via the (0,0) check below, but its own submenus still
        # landed wrong, needing the SAME translation, not a fresh
        # (0,0)-triggered one). Once a pid is confirmed broken (because SOME
        # menu belonging to it reported exactly (0,0)), the same correction
        # applies to every menu/submenu sharing that pid, checked first
        # before even looking at the new menu's own position.
        #
        # We only remember THAT a pid is broken, not the offset value itself
        # — the offset is re-read from self._window_tracker.client_offset_by_pid
        # on every lookup, since that dict is live-updated by WindowTracker
        # as windows move. Caching the offset value froze it at whatever the
        # window's position was the first time a menu for that pid was seen,
        # so moving the window afterwards left badges drawn at the old,
        # stale position (confirmed live).
        self._broken_pids = set()

    def start(self):
        listener = Atspi.EventListener.new(self._on_event)
        listener.register("object:state-changed:showing")
        print("[menu-badge] listening for object:state-changed:showing")

    def _resolve_correction_offset(self, menu):
        """Returns (offset, pid) for menu. If menu's pid was already
        confirmed broken by an earlier (sub)menu, reuses that cached offset
        directly — menu's OWN position isn't necessarily (0,0) even when it
        needs the same correction (see class docstring note above).
        Otherwise checks menu's own screen position: if exactly (0,0),
        resolves + caches a real offset via the KWin tracker's pid->offset
        map (deckery_filter.py's mechanism for regular windows, reused
        here)."""
        try:
            pid = Atspi.Accessible.get_process_id(menu)
        except Exception:
            pid = None
        if pid is None or pid < 0:
            return (0, 0), pid

        if pid in self._broken_pids:
            return self._window_tracker.client_offset_by_pid.get(pid, (0, 0)), pid

        try:
            rect = Atspi.Component.get_extents(menu, Atspi.CoordType.SCREEN)
        except Exception:
            return (0, 0), pid
        if rect.x > _BROKEN_POSITION_THRESHOLD or rect.y > _BROKEN_POSITION_THRESHOLD:
            return (0, 0), pid

        offset = self._window_tracker.client_offset_by_pid.get(pid)
        if offset is None:
            print(f"[menu-badge] menu position looks broken (0,0) but no "
                  f"tracker offset found for pid={pid}")
            return (0, 0), pid
        print(f"[menu-badge] pid={pid} confirmed broken (0,0) — will read "
              f"its offset live from the KWin tracker from now on")
        self._broken_pids.add(pid)
        return offset, pid

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
                _, pid = self._resolve_correction_offset(event.source)
                self._menu_pid[h] = pid
                try:
                    name = event.source.get_name() or ""
                except Exception:
                    name = ""
                print(f"[menu-badge] menu OPEN: {h} {name!r} (role={role.value_name})")
                self._on_change()
        else:
            if h in self._open_menus:
                del self._open_menus[h]
                self._menu_pid.pop(h, None)
                print(f"[menu-badge] menu CLOSED: {h}")
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
                    self._menu_pid.pop(h, None)
                    continue
                if not state_set.contains(Atspi.StateType.SHOWING):
                    print(f"[menu-badge] menu {h} no longer SHOWING on live "
                          f"check (missed its close event) — dropping")
                    del self._open_menus[h]
                    self._menu_pid.pop(h, None)
                    continue
                n = menu.get_child_count()
            except Exception:
                continue
            pid = self._menu_pid.get(h)
            if pid in self._broken_pids:
                off_x, off_y = self._window_tracker.client_offset_by_pid.get(pid, (0, 0))
            else:
                off_x, off_y = (0, 0)
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
        print(f"[menu-badge] refresh: {len(self.items)} item(s) to badge")
        self.drawing_area.queue_draw()

    def _draw(self, area, cr, width, height, data):
        if self.get_surface():
            self.get_surface().set_input_region(cairo.Region())
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
        tracker = MenuTracker(on_change=overlay.refresh, window_tracker=window_tracker)
        overlay.tracker = tracker

        # Without this, a moved window's corrected offset only reaches the
        # screen via the 1s safety-net poll below or the next menu open/close
        # — visible live as "badge draws at the stale position, then jumps a
        # second later" (deckery#28). Redraw the instant KWin's geometry push
        # arrives instead, same thread/context as everything else here so a
        # direct call (no idle_add dance) is safe.
        window_tracker._on_update = overlay.refresh
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
        GLib.timeout_add_seconds(1, lambda: (overlay.refresh(), True)[1])

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
