#!/usr/bin/env python3
"""Ad-hoc diagnostic (MENU_HIJACK_DESIGN.md): runs continuously, logs every
object:state-changed:showing event, and the instant a MENU/POPUP_MENU
becomes showing, dumps its full recursive subtree (role/name/extents/
enabled/depth for every descendant, not just direct children). Event-driven
— no timing/polling races, just leave it running and interact normally."""

import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

MENU_ROLES = (Atspi.Role.MENU, Atspi.Role.POPUP_MENU)


def ts():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def dump(node, depth=0, max_depth=12):
    if depth > max_depth:
        return
    indent = "  " * depth
    try:
        role = node.get_role_name() or "?"
        name = node.get_name() or ""
        ss = node.get_state_set()
        showing = ss.contains(Atspi.StateType.SHOWING)
        enabled = ss.contains(Atspi.StateType.ENABLED)
        rect = Atspi.Component.get_extents(node, Atspi.CoordType.SCREEN)
        print(f"{indent}[{role}] {name!r} showing={showing} enabled={enabled} "
              f"rect=({rect.x},{rect.y},{rect.width},{rect.height})")
    except Exception as e:
        print(f"{indent}<error: {e}>")
        return
    try:
        n = node.get_child_count()
    except Exception:
        return
    for i in range(n):
        try:
            c = node.get_child_at_index(i)
            if c is not None:
                dump(c, depth + 1, max_depth)
        except Exception:
            pass


def on_event(event):
    try:
        role = event.source.get_role()
        name = event.source.get_name() or ""
    except Exception:
        role, name = None, ""
    is_showing = bool(event.detail1)
    print(f"[{ts()}] event: {event.type} role={role.value_name if role else '?'} "
          f"name={name!r} detail1={event.detail1}")

    if role in MENU_ROLES and is_showing:
        try:
            app = Atspi.Accessible.get_application(event.source)
            app_name = app.get_name() if app else "?"
        except Exception:
            app_name = "?"
        print(f"  === FULL DUMP: menu {name!r} in app {app_name!r} ===")
        dump(event.source, 0)
        print()


def main():
    listener = Atspi.EventListener.new(on_event)
    listener.register("object:state-changed:showing")
    print(f"[{ts()}] listening — interact normally, dumps happen automatically "
          f"the instant a menu/popup-menu opens. Ctrl+C to stop.")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
