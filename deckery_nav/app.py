"""Navigation prototype app: backend client + makima input + pointer + overlay.

One click-through layer-shell overlay per monitor (like tools/v2_viewer.py)
showing the selection, where the stick would jump, the finger point on the
pad, and which presets are active. Control (switch presets, read state):
D-Bus org.deckery.Nav /Nav, see ctl.py."""

import json
import math
import os
import signal
import sys
import time

import cairo
import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell

from deckery_atspi.geometry import Rect

from .makima_input import ExportKeeper, StateFile, makima_command
from .nav import Nav
from .presets import load_presets

BACKEND_INTERFACE = "org.deckery.Atspi.Elements"
NAV_NAME = "org.deckery.Nav"
NAV_PATH = "/Nav"
NAV_XML = """
<node>
  <interface name="org.deckery.Nav">
    <method name="Set"><arg type="s" name="kind" direction="in"/><arg type="s" name="name" direction="in"/>
      <arg type="b" name="ok" direction="out"/></method>
    <method name="Step"><arg type="s" name="kind" direction="in"/><arg type="i" name="step" direction="in"/>
      <arg type="s" name="name" direction="out"/></method>
    <method name="List"><arg type="s" name="presets" direction="out"/></method>
    <method name="State"><arg type="s" name="state" direction="out"/></method>
  </interface>
</node>
"""

INPUT_HZ = 125

SELECT = (0.2, 0.85, 1.0)
AIM = (1.0, 0.75, 0.2)
FINGER = (1.0, 0.35, 0.6)


class Overlay(Gtk.Window):
    def __init__(self, app, monitor):
        super().__init__(application=app)
        self._app = app
        self.monitor = monitor
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_namespace(self, "deckery-nav")
        Gtk4LayerShell.set_monitor(self, monitor)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(self, edge, True)
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self._draw, None)
        self.set_child(self.area)

    def _draw(self, area, cr, width, height, _data):
        surface = self.get_surface()
        if surface:
            surface.set_input_region(cairo.Region())
        geo = self.monitor.get_geometry()
        cr.translate(-geo.x, -geo.y)
        self._app.draw(cr)
        cr.identity_matrix()
        self._app.draw_status(cr, width, height)


class NavApp(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id=None, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args = args
        self.presets_path = args.presets
        self.presets_mtime = os.stat(self.presets_path).st_mtime_ns
        self.nav = None
        self.overlays = []
        self.input = StateFile(args.makima_state)
        self.keeper = ExportKeeper() if args.ensure_analog_export else None
        self.pointer = None
        self.pointer_error = None
        self.backend_up = False
        self._dirty = True

    # ── setup ────────────────────────────────────────────────────────────
    def do_activate(self):
        monitors = Gdk.Display.get_default().get_monitors()
        rects = []
        for i in range(monitors.get_n_items()):
            m = monitors.get_item(i)
            g = m.get_geometry()
            rects.append(Rect(g.x, g.y, g.width, g.height))
            overlay = Overlay(self, m)
            overlay.present()
            self.overlays.append(overlay)
        self.nav = Nav(load_presets(self.presets_path), rects)
        if not self.args.no_pointer:
            try:
                from .pointer import Pointer
                self.pointer = Pointer()
            except Exception as e:     # noqa: BLE001 — keep running, show it
                self.pointer_error = str(e)
                print(f"deckery-nav: no pointer control: {e}", file=sys.stderr)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._bus = bus
        bus.signal_subscribe(self.args.bus_name, BACKEND_INTERFACE, "Changed", "/Elements", None,
                             Gio.DBusSignalFlags.NONE, self._on_changed)
        Gio.bus_watch_name(Gio.BusType.SESSION, self.args.bus_name, Gio.BusNameWatcherFlags.NONE,
                           self._on_backend_up, self._on_backend_down)
        node = Gio.DBusNodeInfo.new_for_xml(NAV_XML)
        bus.register_object(NAV_PATH, node.interfaces[0], self._on_call, None, None)
        Gio.bus_own_name_on_connection(bus, NAV_NAME, Gio.BusNameOwnerFlags.REPLACE, None, None)
        GLib.timeout_add(1000 // INPUT_HZ, self._on_tick)
        GLib.timeout_add(500, self._check_presets_file)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._quit)
        self.hold()
        print(f"deckery-nav: up — stick: {self.nav.relative.name}, pad: {self.nav.absolute.name}", flush=True)

    def _quit(self):
        if self.keeper is not None and self.keeper.want_off_at_exit(self.input.snapshot):
            makima_command("analog-state-export off")
        self.quit()
        return GLib.SOURCE_REMOVE

    # ── backend ──────────────────────────────────────────────────────────
    def _on_backend_up(self, connection, name, owner):
        self.backend_up = True
        connection.call(self.args.bus_name, "/Elements", BACKEND_INTERFACE, "Get", None,
                        GLib.VariantType("(us)"), Gio.DBusCallFlags.NONE, 2000, None,
                        lambda c, res: self._set_elements(c.call_finish(res).unpack()[1]))

    def _on_backend_down(self, connection, name):
        self.backend_up = False
        self._set_elements("[]")

    def _on_changed(self, connection, sender, path, interface, signal_name, params):
        self._set_elements(params.unpack()[1])

    def _set_elements(self, text):
        self.nav.set_elements(json.loads(text))
        self._redraw()

    # ── input loop ───────────────────────────────────────────────────────
    def _on_tick(self):
        now = time.monotonic()
        fresh = self.input.poll()
        snap = self.input.snapshot
        if self.keeper is not None and self.keeper.want_on(snap, now):
            makima_command("analog-state-export on")
        if fresh or self.nav.gate.armed is False:
            # A held stick repeats without new readings: step it anyway.
            had_aim = self.nav.aim is not None
            warp = self.nav.on_input(snap, now)
            if warp is not None and self.pointer is not None:
                try:
                    self.pointer.warp(*warp)
                except Exception as e:     # noqa: BLE001
                    self.pointer_error = str(e)
            if fresh or warp is not None or had_aim != (self.nav.aim is not None):
                self._redraw()
        return GLib.SOURCE_CONTINUE

    def _check_presets_file(self):
        try:
            mtime = os.stat(self.presets_path).st_mtime_ns
            if mtime != self.presets_mtime:
                self.presets_mtime = mtime
                self.nav.set_presets(load_presets(self.presets_path))
                print("deckery-nav: presets reloaded", flush=True)
                self._redraw()
        except Exception as e:     # noqa: BLE001 — a half-saved file: keep the old ones
            print(f"deckery-nav: presets not reloaded: {e}", file=sys.stderr)
        return GLib.SOURCE_CONTINUE

    # ── control ──────────────────────────────────────────────────────────
    def _on_call(self, connection, sender, path, interface, method, params, invocation):
        nav = self.nav
        if method == "Set":
            kind, name = params.unpack()
            ok = kind in ("relative", "absolute") and nav.select_preset(kind, name=name)
            invocation.return_value(GLib.Variant("(b)", (ok,)))
        elif method == "Step":
            kind, step = params.unpack()
            if kind not in ("relative", "absolute"):
                invocation.return_dbus_error("org.deckery.Nav.Error", "kind: relative|absolute")
                return
            nav.select_preset(kind, step=step)
            invocation.return_value(GLib.Variant("(s)", (getattr(nav, kind).name,)))
        elif method == "List":
            data = {k: [{"name": p.name, "label": p.label} for p in getattr(nav.presets, k)]
                    for k in ("relative", "absolute")}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(data),)))
        else:
            state = nav.state()
            state["pointer_error"] = self.pointer_error
            state["makima_analog"] = self.input.snapshot.analog
            invocation.return_value(GLib.Variant("(s)", (json.dumps(state),)))
        self._redraw()

    # ── drawing ──────────────────────────────────────────────────────────
    def _redraw(self):
        for o in self.overlays:
            o.area.queue_draw()

    def draw(self, cr):
        nav = self.nav
        if nav is None:
            return
        if nav.presets.show_all:
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.set_line_width(1)
            for t in nav.targets:
                for v in t.visible:
                    cr.rectangle(v.x + 0.5, v.y + 0.5, v.w - 1, v.h - 1)
            cr.stroke()
        if nav.touching and nav.area is not None:
            a = nav.area
            screen = nav._screen()
            if screen is None or a != screen:
                cr.set_source_rgba(*FINGER, 0.5)
                cr.set_line_width(1.5)
                cr.set_dash([6, 4])
                cr.rectangle(a.x + 1, a.y + 1, a.w - 2, a.h - 2)
                cr.stroke()
                cr.set_dash([])
        if nav.aim is not None and nav.selection is not None:
            ox, oy = nav.selection.main.center
            cr.set_source_rgba(*AIM, 0.9)
            cr.set_line_width(2)
            ex, ey = ox + 60 * math.cos(nav.aim.angle), oy + 60 * math.sin(nav.aim.angle)
            cr.move_to(ox, oy)
            cr.line_to(ex, ey)
            cr.stroke()
            if nav.aim.preview is not None:
                cr.set_dash([5, 3])
                for v in nav.aim.preview.visible:
                    cr.rectangle(v.x - 1, v.y - 1, v.w + 2, v.h + 2)
                cr.stroke()
                cr.set_dash([])
        s = nav.selection
        if s is not None:
            cr.set_source_rgba(*SELECT, 0.95)
            cr.set_line_width(3)
            for v in s.visible:
                cr.rectangle(v.x - 2, v.y - 2, v.w + 4, v.h + 4)
            cr.stroke()
            cr.set_source_rgba(*SELECT, 0.15)
            for v in s.visible:
                cr.rectangle(v.x, v.y, v.w, v.h)
            cr.fill()
        if nav.finger_point is not None:
            fx, fy = nav.finger_point
            cr.set_source_rgba(*FINGER, 0.9)
            cr.set_line_width(2)
            cr.arc(fx, fy, 6, 0, 2 * math.pi)
            cr.stroke()
            snap = nav.absolute.params.snap_px
            if 0 < snap < 400:
                cr.set_source_rgba(*FINGER, 0.25)
                cr.set_line_width(1)
                cr.arc(fx, fy, snap, 0, 2 * math.pi)
                cr.stroke()

    def draw_status(self, cr, width, height):
        nav = self.nav
        if nav is None:
            return
        lines = [f"Stick: {nav.relative.label}   ·   Pad: {nav.absolute.label}"]
        s = nav.selection
        if s is not None:
            lines.append(f"{s.role}: {s.name[:60]}" if s.name else s.role)
        problems = []
        if not self.backend_up:
            problems.append("Backend nicht da")
        if not self.input.snapshot.analog:
            problems.append("keine Analogwerte von makima (HUD zu?"
                            + (")" if self.keeper is None else ", wird eingeschaltet)"))
        if self.pointer is None:
            problems.append("Zeiger aus" + (f": {self.pointer_error}" if self.pointer_error else ""))
        if problems:
            lines.append("⚠ " + " · ".join(problems))
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        y = height - 12 - 18 * (len(lines) - 1)
        for line in lines:
            ext = cr.text_extents(line)
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.rectangle(8, y - 14, ext.x_advance + 10, 19)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(13, y)
            cr.show_text(line)
            y += 18
