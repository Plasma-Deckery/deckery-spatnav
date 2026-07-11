#!/usr/bin/env python3
"""
prototype_atspi_overlay.py — Standalone prototype, run inside the deckery
distrobox container (Gtk4LayerShell + Atspi GI bindings live there).

Fullscreen transparent overlay (GTK4 + Gtk4LayerShell) that draws a
rectangle around every FOCUSABLE/SHOWING/ENABLED AT-SPI2 element currently
found in windows that are actually visible on the current active virtual
desktop.

As of deckery#28's event-driven rewrite, the AT-SPI tree itself is built
ONCE at startup (deckery_events.TreeCache.build_full(), off the main
thread) and thereafter kept current incrementally by AT-SPI events
(children-changed, state-changed:showing/enabled, window create/destroy/
activate/deactivate) — not by a periodic full re-walk. What still runs on
every redraw (tracker update OR AT-SPI event) is deckery_walk.render_tree():
a cheap, pure, zero-AT-SPI-call pass that applies our own KWin-based
virtual-desktop visibility filtering + on-screen offset/scale
(deckery_filter.py) to the cached tree. See deckery_events.py's and
deckery_walk.py's module docstrings for the full split rationale.

This file is deliberately thin: it's just the GTK/layer-shell bootstrap and
the wiring between the separated concern modules —
  prototype_atspi_scan.py  - raw AT-SPI tree walk for one window
  deckery_tracker.py       - KWin window-tracker D-Bus service + state
  deckery_filter.py        - visibility filtering + coordinate/scale fixup
  deckery_walk.py          - raw walk (walk_app/build_full_tree) + render_tree
  deckery_events.py        - TreeCache: event-driven incremental tree updates
  deckery_draw.py          - cairo rendering
  deckery_a11y.py          - forces Chromium/Electron to build AT-SPI trees
See each module's docstring for the reasoning behind its piece.

⚠️  AT-SPI COVERAGE LIMITATIONS (see ATSPI_LIMITATIONS.md):
  • Electron apps (Claude, Discord, VSCode, etc.) register windows but don't
    expose their internal UI elements. The overlay will show the window frame
    but 0 focusable elements inside.
  • KDE context menus and window menus are transient popups not tracked by
    KWin's workspace.windowList() — won't appear in the overlay.
  • Some apps don't support AT-SPI at all and won't appear in the overlay.

Single-monitor assumption: layer-shell surfaces are bound to one Wayland
output. On multi-monitor setups this would need one overlay window per
output; out of scope for this prototype.

Run:
    distrobox enter deckery -- python3 prototype_atspi_overlay.py

(The companion KWin script must already be installed + enabled on the host;
see ~/.local/share/kwin/scripts/deckery-windowtracker.)

Ctrl+C (in the terminal) or close the window to quit.
"""

import os
import sys

# GTK4 dropped native layer-shell support; gtk4-layer-shell only works by
# LD_PRELOAD-ing libgtk4-layer-shell.so to intercept GDK surface creation
# before GTK initializes. Without it, Gtk4LayerShell.init_for_window() below
# fails *silently* (only a "GtkWindow is not a layer surface" warning on
# stderr) and the window falls back to being a plain xdg-toplevel — which
# looks fine at a glance but breaks two things: it can be closed/hidden by
# the WM like any normal window, and it's tied to a single virtual desktop
# instead of being a desktop-independent compositor overlay (confirmed via
# direct comparison against the working deckery-hud launcher, which sets
# this same variable — deckery#28). Re-exec once with it set rather than
# relying on every caller remembering `LD_PRELOAD=... python3 ...`.
_LAYER_SHELL_PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if _LAYER_SHELL_PRELOAD not in os.environ.get("LD_PRELOAD", "").split(":"):
    existing = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = (
        f"{_LAYER_SHELL_PRELOAD}:{existing}" if existing else _LAYER_SHELL_PRELOAD
    )
    # Preserve unbuffered stdout across the re-exec even if the original
    # invocation used `python3 -u ...` — that flag lives on the interpreter
    # command line, not in sys.argv, so it would otherwise get silently
    # dropped here and log lines would appear to "go missing" until the
    # process exits or the buffer fills.
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(sys.executable, [sys.executable] + sys.argv)

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Atspi", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk
import cairo

import deckery_draw as draw
import deckery_walk as walk
from deckery_cache import ElementCache, ATSPI_LOCK
from deckery_events import EventRouter
from deckery_a11y import enable_chromium_accessibility
from deckery_tracker import WindowTracker


class OverlayWindow(Gtk.Window):
    def __init__(self):
        super().__init__()

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

        self.elements = []  # list of (depth, role, name, extents)
        self.last_walk_ms = 0.0
        self._skipped_windows = 0
        self._tree_ready = False  # True once the initial build_full() lands

        self.tracker = WindowTracker(on_update=self._render)
        self.tracker.start()
        enable_chromium_accessibility()

        # ElementCache owns the set of relevant AT-SPI elements.
        # build_initial() runs once on a background thread (full desktop walk
        # to prime Orca's AXCacheManager). After that, EventRouter routes
        # individual AT-SPI events directly to ElementCache's O(1)/O(subtree)
        # handlers — no full app re-walk per event (deckery_cache.py).
        self.element_cache = ElementCache()
        self.event_router = EventRouter(self.element_cache)
        threading.Thread(target=self._build_initial_cache, daemon=True).start()

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._draw, None)
        self.set_child(self.drawing_area)
        # Draw the loading state immediately so it's visible during the
        # 5-8 s build_initial() background walk.
        GLib.idle_add(self.drawing_area.queue_draw)

        css = Gtk.CssProvider()
        css.load_from_data(b"window.overlay-transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _build_initial_cache(self):
        """Background thread: one-time full desktop walk to populate
        ElementCache and prime Orca's AXCacheManager."""
        with ATSPI_LOCK:
            meta, elements = self.element_cache.build_initial()
        GLib.idle_add(self._apply_initial_cache, meta, elements)

    def _apply_initial_cache(self, meta, elements):
        """Main thread: apply initial cache and start event listener."""
        self.element_cache.apply_initial(meta, elements)
        self._tree_ready = True
        # EventRouter must be started on the main thread (AT-SPI event
        # dispatch goes through the GLib main loop).
        self.event_router.start(on_change=self._render)
        self._render()
        return False  # one-shot idle source

    def _render(self):
        if not self._tree_ready:
            return
        result = walk.render_tree(self.tracker, self.element_cache)
        self.elements = result.elements
        self._skipped_windows = result.skipped_windows
        self.last_walk_ms = result.elapsed_ms
        fallback_note = (
            f", {result.fallback_offsets} offset fallback(s)" if result.fallback_offsets else ""
        )
        t = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
        print(
            f"[overlay] {ts} re-rendered: {len(self.elements)} elements in "
            f"{self.last_walk_ms:.0f}ms{fallback_note}"
        )
        self.drawing_area.queue_draw()

    def _draw(self, area, cr, width, height, data):
        draw.reset_click_through(self.get_surface())
        if not self._tree_ready:
            draw.draw_hud(cr, "deckery spatial-nav — building AT-SPI tree…")
            return
        draw.draw_elements(cr, self.elements)
        tracker_status = (
            "tracker: live" if self.tracker.visible_pids is not None else "tracker: NO DATA (unfiltered)"
        )
        draw.draw_hud(
            cr,
            f"deckery spatial-nav prototype — {len(self.elements)} elements, "
            f"last render {self.last_walk_ms:.0f}ms, event-driven tree, "
            f"{self._skipped_windows} hidden windows skipped, {tracker_status}",
        )


def main():
    app = Gtk.Application(application_id="dev.deckery.overlay_prototype")

    def on_activate(app):
        win = OverlayWindow()
        win.set_application(app)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
