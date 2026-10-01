#!/usr/bin/env python3
"""
cursor_nav_tray.py — minimal tray icon to control the cursor-nav prototype
(main.py — see deckery_backend.py for why the entry point is main.py and
not cursor_nav_prototype.py directly: only main.py owns the single shared
AT-SPI backend).

Runs natively on the host (GTK3 + AyatanaAppIndicator3, same libraries the
real production deckery-tray uses — see ~/Programming/deckery/tray/) rather
than in the deckery distrobox container: GTK3 and GTK4 (which
cursor_nav_prototype.py uses for its layer-shell overlay window) can't be
loaded in the same process, so this has to be a separate process regardless,
and system-tray integration is a host-level concern anyway.

Deliberately NOT touching the real deckery-tray — that's a full production
system (systemd service, RPM packaging, its own docs) and this is a
throwaway prototype control, not a feature to merge into it.

Menu: Aktivieren/Deaktivieren (toggle), Neu starten, Beenden, plus a
Strategie submenu that flips the running process's navigation model LIVE
via the org.deckery.CursorNav D-Bus service (SetStrategy) that
cursor_nav_prototype.py registers — no restart needed to compare
SpringAnchorStrategy vs SemanticGainFieldStrategy. The D-Bus session bus is
shared between host and the deckery container (same as
org.deckery.WindowTracker already relies on), so this call needs no
distrobox wrapping, unlike process start/stop which does.
"""

import os
import signal
import subprocess

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, AyatanaAppIndicator3, GLib, Gio

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MAIN_PATH = os.path.join(_SCRIPT_DIR, "main.py")
_LAUNCH_CMD = [
    "distrobox-host-exec",
    "distrobox",
    "enter",
    "deckery",
    "--",
    "python3",
    "-u",
    _MAIN_PATH,
]
_KILL_PATTERN = "main.py"  # matched via pkill -f against the running process's cmdline

_CURSOR_NAV_BUS_NAME = "org.deckery.CursorNav"
_CURSOR_NAV_OBJECT_PATH = "/CursorNav"
_CURSOR_NAV_INTERFACE = "org.deckery.CursorNav"
_STRATEGIES = [
    ("semantic", "Semantic Gain Field"),
    ("spring", "Spring Anchor"),
]


class CursorNavTray:
    def __init__(self):
        self.proc = None

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "cursor-nav-prototype",
            "input-gaming-symbolic",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()

        self.toggle_item = Gtk.MenuItem(label="Aktivieren")
        self.toggle_item.connect("activate", self._on_toggle)
        self.menu.append(self.toggle_item)

        restart_item = Gtk.MenuItem(label="Neu starten")
        restart_item.connect("activate", self._on_restart)
        self.menu.append(restart_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        strategy_submenu = Gtk.Menu()
        radio_group = None
        for name, label in _STRATEGIES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(radio_group, label)
            if radio_group is None:
                radio_group = item
            if name == "semantic":  # matches DEFAULT_STRATEGY_NAME in cursor_nav_prototype.py
                item.set_active(True)
            item.connect("toggled", self._on_strategy_toggled, name)
            strategy_submenu.append(item)
        strategy_item = Gtk.MenuItem(label="Strategie")
        strategy_item.set_submenu(strategy_submenu)
        self.menu.append(strategy_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Beenden")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        # Catch the overlay process dying on its own (crash, killed
        # elsewhere) so the tray label doesn't lie about the state.
        GLib.timeout_add_seconds(2, self._poll_process)

    def _is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        if self._is_running():
            return
        print("[tray] starting main.py")
        self.proc = subprocess.Popen(
            _LAUNCH_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._update_label()

    def _stop(self):
        if not self._is_running():
            self.proc = None
            self._update_label()
            return
        print("[tray] stopping main.py")
        # distrobox-host-exec/distrobox enter wrap the real python3 process
        # in a shell chain (see the session's earlier pgrep dumps) — a plain
        # terminate() on self.proc only kills the outermost wrapper, not the
        # actual GTK app. Kill by process-group instead: Popen with no
        # special args still gets its own pgid on Linux by default only if
        # start_new_session=True was passed, which we didn't — so fall back
        # to pkill on the known script path, same approach used earlier in
        # this session to reliably stop the overlay.
        try:
            subprocess.run(
                ["distrobox-host-exec", "distrobox", "enter", "deckery", "--",
                 "pkill", "-f", _KILL_PATTERN],
                timeout=5,
            )
        except Exception as e:
            print(f"[tray] pkill failed: {e}")
        try:
            self.proc.wait(timeout=3)
        except Exception:
            pass
        self.proc = None
        self._update_label()

    def _update_label(self):
        self.toggle_item.set_label("Deaktivieren" if self._is_running() else "Aktivieren")

    def _on_strategy_toggled(self, item, name):
        if not item.get_active():
            return  # RadioMenuItem fires toggled for both the newly-active AND newly-inactive item
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                _CURSOR_NAV_BUS_NAME,
                _CURSOR_NAV_OBJECT_PATH,
                _CURSOR_NAV_INTERFACE,
                "SetStrategy",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            print(f"[tray] switched strategy to {name!r}")
        except Exception as e:
            print(f"[tray] SetStrategy({name!r}) failed (is main.py running?): {e}")

    def _poll_process(self):
        self._update_label()
        return True  # keep polling

    def _on_toggle(self, _item):
        if self._is_running():
            self._stop()
        else:
            self._start()

    def _on_restart(self, _item):
        self._stop()
        self._start()

    def _on_quit(self, _item):
        self._stop()
        Gtk.main_quit()


def main():
    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    CursorNavTray()
    Gtk.main()


if __name__ == "__main__":
    main()
