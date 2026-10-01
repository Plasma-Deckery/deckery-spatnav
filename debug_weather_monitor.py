#!/usr/bin/env python3
"""Ad-hoc monitor (deckery#28): watches the 'Weather Report' tray button's
AT-SPI state every 100ms for 30s, logging every change, to catch a transient
showing/enabled flicker live instead of a single static snapshot."""

import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState
from vendor.orca_ax.ax_component import AXComponent

DURATION_S = 30
POLL_S = 0.1


def find_weather_button():
    d = Atspi.get_desktop(0)
    for i in range(d.get_child_count()):
        a = d.get_child_at_index(i)
        if a and (Atspi.Accessible.get_name(a) or "") == "plasmashell":
            for j in range(a.get_child_count()):
                win = a.get_child_at_index(j)
                if win is None:
                    continue
                found = _find(win, 0)
                if found is not None:
                    return found
    return None


def _find(n, depth):
    if depth > 10:
        return None
    try:
        if (Atspi.Accessible.get_name(n) or "") == "Weather Report" and \
           (Atspi.Accessible.get_role_name(n) or "") == "button":
            return n
    except Exception:
        return None
    try:
        cnt = n.get_child_count()
    except Exception:
        return None
    for i in range(cnt):
        try:
            c = n.get_child_at_index(i)
            if c is not None:
                r = _find(c, depth + 1)
                if r is not None:
                    return r
        except Exception:
            pass
    return None


def snapshot(btn):
    if AXObject.is_dead(btn):
        return ("DEAD", None, None, None)
    ss = AXObject.get_state_set(btn)
    showing = AXUtilitiesState.is_showing(btn, ss)
    enabled = AXUtilitiesState.is_enabled(btn, ss)
    rect = AXComponent.get_rect(btn)
    return (showing, enabled, rect.width, rect.height)


def main():
    btn = find_weather_button()
    if btn is None:
        print("Weather Report button not found")
        return
    print("found Weather Report button, monitoring for", DURATION_S, "s...")

    last = None
    t_end = time.time() + DURATION_S
    changes = 0
    while time.time() < t_end:
        if AXObject.is_dead(btn):
            cur = ("DEAD", None, None, None)
            if last != cur:
                print(f"[{time.strftime('%H:%M:%S')}] -> DEAD (object destroyed) — re-searching")
                btn = find_weather_button()
                if btn is None:
                    print("  could not find a replacement object")
                    break
                cur = snapshot(btn)
        else:
            cur = snapshot(btn)
        if cur != last:
            changes += 1
            print(f"[{time.strftime('%H:%M:%S.%f')[:-3]}] state changed: "
                  f"showing={cur[0]} enabled={cur[1]} size={cur[2]}x{cur[3]}")
            last = cur
        time.sleep(POLL_S)

    print(f"\ndone — {changes} state changes observed over {DURATION_S}s")


if __name__ == "__main__":
    main()
