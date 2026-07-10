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

Deliberately setting IsEnabled, NOT ScreenReaderEnabled: on GNOME,
ScreenReaderEnabled is mirrored by gsd-a11y-settings into actually
auto-starting Orca as a side effect — a real screen reader we don't want
running just to read extents. IsEnabled is the narrower "some AT is active"
bit Mozilla/Chromium key their tree-building off of, without that side
effect. (On this system's KDE Plasma session there's no gsd-a11y-settings to
trigger anyway, but IsEnabled is the correct property regardless of desktop
environment — see https://github.com/trycua/cua/issues/2010 for the
equivalent problem in a similar computer-use-agent tool, and the Debian
accessibility mailing list thread on the IsEnabled/ScreenReaderEnabled
distinction.)

One-shot, fire-and-forget at startup: once Chromium sees IsEnabled=True it
keeps building trees for the rest of the process lifetime, no need to repeat
this call.
"""

from gi.repository import GLib, Gio


def enable_chromium_accessibility():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.a11y.Bus",
            "/org/a11y/bus",
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant(
                "(ssv)",
                ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True)),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        print("[a11y] set org.a11y.Status IsEnabled=True (enables Chromium/Electron AT-SPI trees)")
    except Exception as e:
        print(f"[a11y] failed to set org.a11y.Status IsEnabled: {e}")
