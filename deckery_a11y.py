#!/usr/bin/env python3
"""
deckery_a11y.py — force Chromium/Electron apps to build their AT-SPI tree
(deckery#28).

Chromium (and therefore every Electron app, e.g. Claude Desktop, VS Code)
keeps its AT-SPI accessibility tree disabled by default for performance —
walking it costs nothing (0 focusable elements) until Chromium detects that
a "real" assistive technology is present. Confirmed for this repo
(deckery#28): the "electron" AT-SPI app object and its "Claude" frame both
exist with correct extents, but the frame has zero children no matter how
the tree is walked — Chromium just never built the tree.

Detection works by watching a well-known D-Bus property on the AT-SPI
registry bus (org.a11y.Bus at /org/a11y/bus, interface org.a11y.Status).
Setting IsEnabled=True there is the same signal a real screen reader like
Orca sends on startup; Chromium's Aura Linux accessibility backend
(ui/accessibility/platform/atk_util_auralinux.cc) reacts to it by switching
on full renderer accessibility trees for every open window, matching
Chrome's own --force-renderer-accessibility flag but without needing to
control how the target app is launched (we don't control how Claude Desktop
starts).

Setting both IsEnabled AND ScreenReaderEnabled:
- IsEnabled triggers Chromium/Electron (confirmed deckery#28).
- ScreenReaderEnabled triggers Firefox/Gecko: Firefox's nsAccessibilityService
  specifically watches for ScreenReaderEnabled=True to initialise its full
  AT-SPI tree (tabs, toolbar, back/forward, address bar, page content). Without
  it, only the top-level window frame is registered but zero children are
  exposed — confirmed by AT-SPI inspection showing elements=1 for Firefox.
- Risk on GNOME: ScreenReaderEnabled=True is mirrored by gsd-a11y-settings
  into auto-starting Orca. On this system's KDE Plasma session there is no
  gsd-a11y-settings, so this side-effect does not apply here.

One-shot, fire-and-forget at startup: once Chromium sees IsEnabled=True it
keeps building trees for the rest of the process lifetime, no need to repeat
this call.
"""

from gi.repository import GLib, Gio


def enable_chromium_accessibility():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        for prop, label in (
            ("IsEnabled", "Chromium/Electron"),
            ("ScreenReaderEnabled", "Firefox/Gecko"),
        ):
            bus.call_sync(
                "org.a11y.Bus",
                "/org/a11y/bus",
                "org.freedesktop.DBus.Properties",
                "Set",
                GLib.Variant("(ssv)", ("org.a11y.Status", prop, GLib.Variant("b", True))),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        print("[a11y] set org.a11y.Status IsEnabled+ScreenReaderEnabled=True (enables Chromium/Electron + Firefox AT-SPI trees)")
    except Exception as e:
        print(f"[a11y] failed to set org.a11y.Status: {e}")
