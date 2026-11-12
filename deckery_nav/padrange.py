"""Measure how far the thumb reaches on the left pad, suggest [pad] borders.

    python3 -m deckery_nav.padrange [SECONDS]      (default 20)

Rub along all four edges of the left pad while it runs. Reads makima's state
file like the prototype (temporary workaround, see makima_input.py) and
switches the analog export on if it is off. Prints the extremes and a [pad]
section for presets.toml."""

import sys
import time

from .absolute import edges_from_range
from .makima_input import ExportKeeper, StateFile, makima_command


def main(argv):
    seconds = float(argv[0]) if argv else 20.0
    state = StateFile()
    keeper = ExportKeeper()
    x_min = y_min = 9.0
    x_max = y_max = -9.0
    samples = 0
    print(f"{seconds:.0f} s: am linken Pad alle vier Ränder entlangstreichen …", flush=True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if state.poll():
            pad = state.snapshot.lpad
            if pad.touching:
                samples += 1
                x_min, x_max = min(x_min, pad.x), max(x_max, pad.x)
                y_min, y_max = min(y_min, pad.y), max(y_max, pad.y)
        if keeper.want_on(state.snapshot, time.monotonic()):
            makima_command("analog-state-export on")
        time.sleep(0.005)
    if keeper.want_off_at_exit(state.snapshot):
        makima_command("analog-state-export off")
    if not samples:
        print("keine Berührung gesehen")
        return 1
    print(f"{samples} Werte: x {x_min:+.3f} … {x_max:+.3f}, y {y_min:+.3f} … {y_max:+.3f} (y oben = +)")
    left, right, top, bottom = edges_from_range(x_min, x_max, y_min, y_max)
    print("\n[pad]")
    print(f"edge_left = {left}\nedge_right = {right}\nedge_top = {top}\nedge_bottom = {bottom}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
