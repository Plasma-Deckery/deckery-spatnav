#!/usr/bin/env python3
"""
cursor_nav_prototype.py — minimal gamepad-driven focus navigation frontend.

See CURSOR_NAV_DESIGN.md for the full design brainstorm this implements a
first slice of, and deckery_backend.py for the shared AT-SPI backend this
frontend reads from (NOT its own ElementCache/WindowTracker — running two
independent backends in two processes was a real architecture bug, see that
module's docstring). Entry point is main.py, which owns the one SharedBackend
instance and passes it to CursorNavWindow here.

Deliberately minimal (per CURSOR_NAV_DESIGN.md's phase-1 scope):

  - Left stick read directly from Steam's own synthesized evdev gamepad
    device, looked up BY NAME ("Steam Deck", see _find_stick_device) since
    its /dev/input/eventN path isn't stable across sessions — observed
    swapping with the duplicate "Microsoft X-Box 360 pad 0" compat device
    (which only carries events when a game explicitly requests XInput
    input, not during normal desktop use) between two runs in the same
    day. No HID, no Makima, no InputPlumber involved — a plain, ungrabbed
    evdev read, so this can't conflict with anything else already reading
    that device.
  - Stick reads are pushed to the active strategy continuously (every raw
    ABS_X/ABS_Y event) — the strategy decides its own response curve,
    deadzone and commit threshold. See cursor_nav_strategy.py's
    SpringAnchorStrategy (current default): the dot is always anchored to
    a real element and stretches elastically toward the stick's direction,
    committing to the next element only past ~50% deflection.
  - The actual "where does the dot go / what gets focused" logic lives in
    cursor_nav_strategy.py, NOT here — deliberately a swappable layer,
    since which mapping model is right is still an open question being
    tried out live. This file only wires stick input and the backend's
    candidates into whatever strategy is configured below, and draws the
    result. Swapping models should never require touching this file's
    GTK/input plumbing.
  - No real cursor movement, no click injection, no shader glow — those
    are later phases in CURSOR_NAV_DESIGN.md. A basic flight animation for
    the post-commit re-anchor DOES exist now (SpringAnchorStrategy.tick()),
    driven by a ~60fps GLib timer independent of stick events, only
    redrawing when it actually animates something (see _on_tick). Visual
    feedback is deliberately always-on while iterating: every detected
    candidate faintly (deckery_draw.draw_elements — needed back per live
    feedback, otherwise there's no way to see what navigation is choosing
    between), a thick rectangle around the anchored element, and the dot
    itself at its actual mapped position.

Run: distrobox enter deckery -- python3 main.py
"""

import math

import evdev
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk, Gio

import deckery_draw as draw
from cursor_nav_strategy import SpringAnchorStrategy, SemanticGainFieldStrategy, NavState

# Steam synthesizes this device from the raw Steam Deck controller HID
# report; confirmed live via direct evdev capture as the actually-active
# one for desktop use (a duplicate "Microsoft X-Box 360 pad 0" compat
# device also exists but only carries events when a game explicitly
# requests XInput-style input). Looked up BY NAME, not by /dev/input/eventN
# path — that numbering isn't stable: observed swapping between "Steam
# Deck" and the Xbox-pad duplicate across sessions/reconnects (event7 vs
# event8 flipped between two runs of this exact file in the same day).
STICK_DEVICE_NAME = "Steam Deck"


def _find_stick_device():
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except Exception:
            continue
        if dev.name == STICK_DEVICE_NAME:
            return dev
    return None


# Which navigation-mapping models are selectable — see cursor_nav_strategy.py.
# Live-switchable at runtime via the org.deckery.CursorNav D-Bus service
# below (SetStrategy) instead of an env var + restart, so the tray can flip
# between them with the process already running.
STRATEGIES = {
    "spring": SpringAnchorStrategy,
    "semantic": SemanticGainFieldStrategy,
}
DEFAULT_STRATEGY_NAME = "semantic"

CURSOR_NAV_BUS_NAME = "org.deckery.CursorNav"
CURSOR_NAV_OBJECT_PATH = "/CursorNav"
CURSOR_NAV_INTERFACE_XML = """
<node>
  <interface name="org.deckery.CursorNav">
    <method name="SetStrategy">
      <arg type="s" name="name" direction="in"/>
    </method>
  </interface>
</node>
"""


class CursorNavWindow(Gtk.Window):
    def __init__(self, backend):
        super().__init__()
        self.backend = backend

        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (
            Gtk4LayerShell.Edge.TOP,
            Gtk4LayerShell.Edge.BOTTOM,
            Gtk4LayerShell.Edge.LEFT,
            Gtk4LayerShell.Edge.RIGHT,
        ):
            Gtk4LayerShell.set_anchor(self, edge, True)

        self.set_decorated(False)
        self.add_css_class("overlay-transparent")

        self.nav_strategy = STRATEGIES[DEFAULT_STRATEGY_NAME]()
        self.nav_state = NavState(dot_pos=None, focus_index=None)
        self._last_stick_xy = (0.0, 0.0)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._draw, None)
        self.set_child(self.drawing_area)
        GLib.idle_add(self.drawing_area.queue_draw)

        css = Gtk.CssProvider()
        css.load_from_data(b"window.overlay-transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._open_stick_device()

        # Drives the post-commit flight animation independent of stick
        # events (see SpringAnchorStrategy.tick() — the ball keeps flying
        # to its new anchor even if the stick is held still right after a
        # commit). No-op for strategies without a tick() method.
        self._last_tick_time = GLib.get_monotonic_time()
        GLib.timeout_add(16, self._on_tick)  # ~60fps; _on_tick itself checks hasattr("tick")

        # Called on the GTK main thread every time the backend refreshes
        # its candidate list (see deckery_backend.SharedBackend) — this
        # window never touches AT-SPI itself, just reacts to fresh data.
        self.backend.start(on_candidates=self._on_candidates)

        self._register_dbus_service()

    # ------------------------------------------------------------------
    # Live strategy switching (org.deckery.CursorNav D-Bus service)
    # ------------------------------------------------------------------

    def _register_dbus_service(self):
        Gio.bus_own_name(
            Gio.BusType.SESSION,
            CURSOR_NAV_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_dbus_bus_acquired,
            None,
            self._on_dbus_name_lost,
        )

    def _on_dbus_bus_acquired(self, connection, name):
        node_info = Gio.DBusNodeInfo.new_for_xml(CURSOR_NAV_INTERFACE_XML)
        connection.register_object(
            CURSOR_NAV_OBJECT_PATH,
            node_info.interfaces[0],
            self._on_dbus_method_call,
            None,
            None,
        )
        print(f"[dbus] registered {CURSOR_NAV_OBJECT_PATH} on {name} (known strategies: {list(STRATEGIES)})")

    def _on_dbus_name_lost(self, connection, name):
        # Most likely another cursor-nav instance is still running — same
        # harmless-for-a-prototype situation as WindowTracker's equivalent
        # (deckery_tracker.py): this instance just won't be remote-switchable.
        print(f"[dbus] FAILED to own {name} — is another cursor-nav instance still running?")

    def _on_dbus_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if method_name != "SetStrategy":
            invocation.return_value(None)
            return
        (name,) = parameters.unpack()
        strategy_cls = STRATEGIES.get(name)
        if strategy_cls is None:
            print(f"[dbus] unknown strategy {name!r}, known: {list(STRATEGIES)}")
            invocation.return_value(None)
            return
        self.nav_strategy = strategy_cls()
        self.nav_state = self.nav_strategy.initial_state(self.backend.candidates, self._screen_center())
        print(f"[dbus] switched to strategy {name!r}")
        self.drawing_area.queue_draw()
        invocation.return_value(None)

    def _on_tick(self):
        now = GLib.get_monotonic_time()
        dt = (now - self._last_tick_time) / 1_000_000.0
        self._last_tick_time = now
        if not hasattr(self.nav_strategy, "tick"):
            return True  # keep the timer alive in case a live switch lands on a strategy that has one
        new_state = self.nav_strategy.tick(self.nav_state, self.backend.candidates, dt)
        # tick() returns the SAME state object (identity, not just equal)
        # when there's nothing to animate (armed, settled, no candidates —
        # see its early returns). Only redraw when it actually did
        # something — this was unconditionally forcing a full redraw of
        # every candidate box at 60fps even at rest, which on a page with
        # 100+ candidates (e.g. System Settings) is a real, continuous
        # CPU/render cost that was never needed most of the time.
        if new_state is not self.nav_state:
            self.nav_state = new_state
            self.drawing_area.queue_draw()
        return True  # keep ticking

    def _on_candidates(self, candidates):
        if self.nav_state.dot_pos is None and candidates:
            self.nav_state = self.nav_strategy.initial_state(candidates, self._screen_center())
        else:
            # Candidate list may have changed (elements appeared/vanished)
            # since the last poll — let the strategy re-derive focus.
            self.nav_state = self.nav_strategy.update_after_render(self.nav_state, candidates)
        self.drawing_area.queue_draw()

    def _screen_center(self):
        surface = self.get_surface()
        if surface is None:
            return 0, 0
        return surface.get_width() / 2.0, surface.get_height() / 2.0

    # ------------------------------------------------------------------
    # Stick input
    # ------------------------------------------------------------------

    def _open_stick_device(self):
        self.stick = _find_stick_device()
        if self.stick is None:
            print(f"[stick] no device named {STICK_DEVICE_NAME!r} found")
            return
        print(f"[stick] reading {self.stick.name!r} at {self.stick.path}")
        GLib.io_add_watch(self.stick.fd, GLib.IO_IN, self._on_stick_readable)

    def _on_stick_readable(self, fd, condition):
        for event in self.stick.read():
            if event.type != evdev.ecodes.EV_ABS:
                continue
            if event.code == evdev.ecodes.ABS_X:
                self._last_stick_xy = (event.value, self._last_stick_xy[1])
            elif event.code == evdev.ecodes.ABS_Y:
                self._last_stick_xy = (self._last_stick_xy[0], event.value)
            else:
                continue
            self._evaluate_stick()
        return True  # keep watching

    def _evaluate_stick(self):
        x, y = self._last_stick_xy
        candidates = self.backend.candidates
        prev_state = self.nav_state
        prev_focus = prev_state.focus_index
        self.nav_state = self.nav_strategy.update(self.nav_state, candidates, x, y)
        s = self.nav_state
        # Diagnostic log for tuning EMA_ALPHA/STRETCH_FRACTION/etc. against
        # real stick data: raw input, filtered input, and every output the
        # strategy derived from it, one line per raw axis event.
        print(
            f"[tick] raw=({x},{y}) smoothed=({s.smoothed_x:.0f},{s.smoothed_y:.0f}) "
            f"armed={s.armed} focus={s.focus_index} target={s.target_index} "
            f"aim_angle={s.aim_angle if s.aim_angle is None else round(s.aim_angle, 3)} "
            f"dot_pos={s.dot_pos if s.dot_pos is None else (round(s.dot_pos[0],1), round(s.dot_pos[1],1))}"
        )
        if s.focus_index is not None and s.focus_index != prev_focus:
            role, name, _ = candidates[s.focus_index]
            print(f"[nav] focused [{role}] {name!r}")
        # update() always constructs a fresh NavState (unlike tick()), and
        # smoothed_x/y drift by tiny amounts on every single call even at
        # rest (an EMA never bit-exactly settles) — comparing the whole
        # state would never suppress anything. Only the fields that
        # actually affect what's drawn matter here: resting-hand tremor on
        # the stick was forcing a full redraw of every candidate box
        # continuously even with the stick untouched, because dot_pos/etc.
        # stayed genuinely identical while the state object itself didn't.
        visible = (s.focus_index, s.dot_pos, s.target_index, s.aim_angle)
        prev_visible = (prev_state.focus_index, prev_state.dot_pos, prev_state.target_index, prev_state.aim_angle)
        if visible != prev_visible:
            self.drawing_area.queue_draw()

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _draw(self, area, cr, width, height, data):
        draw.reset_click_through(self.get_surface())
        if not self.backend.tree_ready:
            draw.draw_hud(cr, "cursor-nav prototype — building AT-SPI tree…")
            return

        candidates = self.backend.candidates
        focus_index = self.nav_state.focus_index
        dot_pos = self.nav_state.dot_pos

        # Every currently-detected candidate, faint — context for what's
        # actually reachable. Needed back per live feedback: without this
        # there's no way to see what the navigation is choosing between.
        draw.draw_elements(cr, [(0, r, n, e) for r, n, e in candidates])

        # Debug view of SemanticGainFieldStrategy's continuous gain field:
        # there's no cheap way to draw the true smooth field in Cairo, so
        # this draws a thin circle at each candidate's influence_radius()
        # instead — the field's natural falloff cutoff — as a stand-in
        # "hard edge" for orientation while the real movement still uses
        # the soft/continuous field underneath. Only for strategies that
        # define influence_radius (duck-typed, same pattern as tick()).
        if hasattr(self.nav_strategy, "influence_radius"):
            cr.set_source_rgba(0.3, 0.7, 1.0, 0.35)
            cr.set_line_width(1.0)
            for _, _, extents in candidates:
                cx = extents.x + extents.width / 2.0
                cy = extents.y + extents.height / 2.0
                r = self.nav_strategy.influence_radius(extents)
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.stroke()

        # Box around the anchored (focused) element — always a real
        # candidate once any exist, per SpringAnchorStrategy.
        if focus_index is not None and focus_index < len(candidates):
            _, _, extents = candidates[focus_index]
            cr.set_source_rgba(1.0, 0.85, 0.1, 0.95)
            cr.set_line_width(4)
            cr.rectangle(extents.x, extents.y, extents.width, extents.height)
            cr.stroke()

        # While aiming (armed + mid-deflection), a thin ray drawn along the
        # EXACT live stick angle — always visible regardless of whether it
        # currently hits anything, so it never lags behind or snaps to a
        # stale direction ("es müsste genau in die Richtung zeigen ... und
        # das tut es nicht" — the previous version drew this line to the
        # found target's center instead of the stick's actual direction).
        aim_angle = self.nav_state.aim_angle
        target_index = self.nav_state.target_index
        if aim_angle is not None and focus_index is not None and focus_index < len(candidates):
            _, _, anchor_extents = candidates[focus_index]
            anchor_cx = anchor_extents.x + anchor_extents.width / 2.0
            anchor_cy = anchor_extents.y + anchor_extents.height / 2.0
            ray_len = math.hypot(width, height)  # long enough to reach any screen edge
            cr.set_source_rgba(1.0, 0.85, 0.1, 0.55)
            cr.set_line_width(1.5)
            cr.move_to(anchor_cx, anchor_cy)
            cr.line_to(anchor_cx + ray_len * math.cos(aim_angle), anchor_cy + ray_len * math.sin(aim_angle))
            cr.stroke()

            # Preview outline on whatever the ray actually hits, if anything.
            if target_index is not None and target_index < len(candidates):
                _, _, target_extents = candidates[target_index]
                cr.set_dash([4, 3])
                cr.rectangle(target_extents.x, target_extents.y, target_extents.width, target_extents.height)
                cr.stroke()
                cr.set_dash([])

        # The dot — its own elastic position, which may be offset from the
        # anchor box's center while the stick is deflected.
        if dot_pos is not None:
            dx = max(0.0, min(dot_pos[0], width))
            dy = max(0.0, min(dot_pos[1], height))
            cr.set_source_rgba(1.0, 0.85, 0.1, 0.95)
            cr.arc(dx, dy, 10, 0, 2 * math.pi)
            cr.fill()

        x, y = self._last_stick_xy
        focused_label = "none"
        if focus_index is not None and focus_index < len(candidates):
            role, name, _ = candidates[focus_index]
            focused_label = f"[{role}] {name[:40]!r}"
        draw.draw_hud(
            cr,
            f"cursor-nav prototype — {len(candidates)} candidates, "
            f"focused: {focused_label}, stick=({x:.0f},{y:.0f})",
        )
