"""Debug viewer for the v2 backend: draws what deckery_atspi publishes.

One click-through layer-shell overlay per monitor. Filled + outlined =
visible part(s) of a target, dot = target point.
No AT-SPI, no KWin — only the backend's /Elements D-Bus API.

Run in the deckery distrobox:
    python3 tools/v2_viewer.py [--bus-name org.deckery.Atspi]"""

import argparse
import json
import os
import sys

_LAYER_SHELL_PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if _LAYER_SHELL_PRELOAD not in os.environ.get("LD_PRELOAD", "").split(":"):
    existing = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = f"{_LAYER_SHELL_PRELOAD}:{existing}" if existing else _LAYER_SHELL_PRELOAD
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(sys.executable, [sys.executable] + sys.argv)

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deckery_draw import DEFAULT_COLOR, ROLE_COLORS

INTERFACE = "org.deckery.Atspi.Elements"


class MonitorOverlay(Gtk.Window):
    def __init__(self, app, monitor, state):
        super().__init__(application=app)
        self._monitor = monitor
        self._state = state
        Gtk4LayerShell.init_for_window(self)
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
        geo = self._monitor.get_geometry()
        cr.translate(-geo.x, -geo.y)
        for e in self._state["elements"]:
            r, g, b, a = ROLE_COLORS.get(e["role"], DEFAULT_COLOR)
            # Only what is actually visible: an outline of the full rect would
            # cross into whatever window covers the rest.
            for x, y, w, h in e["visible"]:
                cr.set_source_rgba(r, g, b, 0.18)
                cr.rectangle(x, y, w, h)
                cr.fill()
                cr.set_source_rgba(r, g, b, 0.9)
                cr.set_line_width(1)
                cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
                cr.stroke()
            tx, ty = e["target"]
            cr.arc(tx, ty, 2.5, 0, 6.2832)
            cr.fill()
        cr.identity_matrix()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_font_size(13)
        cr.move_to(12, height - 12)
        cr.show_text(f"v2 viewer: {len(self._state['elements'])} targets (version {self._state['version']})")


class Viewer(Gtk.Application):
    def __init__(self, bus_name):
        super().__init__(application_id=None, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._bus_name = bus_name
        self.state = {"elements": [], "version": 0}
        self.overlays = []

    def do_activate(self):
        monitors = Gdk.Display.get_default().get_monitors()
        for i in range(monitors.get_n_items()):
            overlay = MonitorOverlay(self, monitors.get_item(i), self.state)
            overlay.present()
            self.overlays.append(overlay)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.signal_subscribe(self._bus_name, INTERFACE, "Changed", "/Elements", None,
                             Gio.DBusSignalFlags.NONE, self._on_changed)
        Gio.bus_watch_name(Gio.BusType.SESSION, self._bus_name, Gio.BusNameWatcherFlags.NONE,
                           self._on_backend_up, self._on_backend_down)
        self.hold()

    def _on_backend_up(self, connection, name, owner):
        connection.call(self._bus_name, "/Elements", INTERFACE, "Get", None,
                        GLib.VariantType("(us)"), Gio.DBusCallFlags.NONE, 2000, None,
                        lambda c, res: self._set(*c.call_finish(res).unpack()))

    def _on_backend_down(self, connection, name):
        self._set(0, "[]")

    def _on_changed(self, connection, sender, path, interface, signal, params):
        self._set(*params.unpack())

    def _set(self, version, text):
        self.state["elements"] = json.loads(text)
        self.state["version"] = version
        for overlay in self.overlays:
            overlay.area.queue_draw()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus-name", default="org.deckery.Atspi")
    args = parser.parse_args()
    return Viewer(args.bus_name).run([])


if __name__ == "__main__":
    sys.exit(main())
