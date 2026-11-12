"""Drive the navigation prototype in the test desktop with a fake makima.

Writes a fake makima-state.json (stick / left pad values), runs
python3 -m deckery_nav against it and the test desktop's backend, and checks
what it selects and where it puts the real (test desktop) pointer. Run in
the box, inside the test desktop:

    DECKERY_TESTDESK=4 tools/testdesk/run_box.sh python3 tools/testdesk/nav_check.py"""

import json
import os
import subprocess
import sys
import time

from gi.repository import Gio, GLib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios  # noqa: E402

REPO = scenarios.REPO
STATE_DIR = os.path.dirname(scenarios.SHOTS)
FAKE = os.path.join(STATE_DIR, "fake-makima-state.json")


def write_input(stick=(0.0, 0.0), pad=(0.0, 0.0, False)):
    state = {"context": {"analog_state_export": True, "paused": False},
             "trackpads": {"left": {"x": pad[0], "y": pad[1], "touching": pad[2], "pressed": False}},
             "sticks": {"lstick": {"x": stick[0], "y": stick[1]}}}
    tmp = FAKE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, FAKE)


def nav_state():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    reply = bus.call_sync("org.deckery.Nav", "/Nav", "org.deckery.Nav", "State", None,
                          GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
    return json.loads(reply.unpack()[0])


def nav_set(kind, name):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.call_sync("org.deckery.Nav", "/Nav", "org.deckery.Nav", "Set", GLib.Variant("(ss)", (kind, name)),
                  GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None)


def push(dx, dy):
    write_input(stick=(dx, dy))
    time.sleep(0.08)
    write_input()
    time.sleep(0.12)
    return nav_state()


def main():
    write_input()
    log = open(os.path.join(STATE_DIR, "nav.log"), "w")
    proc = subprocess.Popen([sys.executable, "-m", "deckery_nav", "--makima-state", FAKE],
                            cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    failures = []
    try:
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline:
            try:
                state = nav_state()
                if state["targets"] > 0:
                    break
            except GLib.Error:
                pass
            time.sleep(0.3)
        print("start:", state)
        if not state or state["targets"] == 0:
            failures.append("no targets")
            return 1
        if state["pointer_error"]:
            failures.append(f"pointer: {state['pointer_error']}")

        # Stick: first push selects, then each push moves.
        trail = []
        for dx, dy in ((0.9, 0), (0.9, 0), (0, -0.9), (0, -0.9), (-0.9, 0), (0, 0.9)):
            s = push(dx, dy)
            trail.append((s["selection"] or {}).get("name") or (s["selection"] or {}).get("role"))
        print("stick trail:", trail)
        if len({t for t in trail if t}) < 3:
            failures.append(f"stick barely moved: {trail}")
        scenarios.screenshot("nav-stick")

        # Held stick repeats (down a list: start top left with the pad).
        write_input(pad=(-0.75, 0.55, True))
        time.sleep(0.12)
        write_input()
        time.sleep(0.12)
        print("held from:", (nav_state()["selection"] or {}).get("name"))
        before = nav_state()["jumps"]
        write_input(stick=(0, -0.9))
        time.sleep(1.0)
        write_input()
        time.sleep(0.1)
        held = nav_state()["jumps"] - before
        print("held 1 s down → jumps:", held)
        if held < 3:
            failures.append(f"held stick repeated only {held}×")

        # Pad: a sweep across the screen selects different targets.
        for preset in ("bildschirm", "zaeh", "fenster"):
            nav_set("absolute", preset)
            seen = []
            for i in range(9):
                x = -0.8 + 0.2 * i
                write_input(pad=(x, 0.3, True))
                time.sleep(0.12)
                s = nav_state()
                seen.append((s["selection"] or {}).get("name") or "-")
            if preset == "bildschirm":
                scenarios.screenshot("nav-pad")
            write_input()
            time.sleep(0.15)
            print(f"pad sweep [{preset}]:", seen)
            if len(set(seen)) < 2:
                failures.append(f"pad sweep [{preset}] selected nothing different: {seen}")
        after_lift = nav_state()
        if after_lift["touching"]:
            failures.append("still touching after lift")
    finally:
        proc.terminate()
        proc.wait(5)
        log.close()
        if failures:
            print("FAILED:", *failures, sep="\n  ")
        else:
            print("ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
