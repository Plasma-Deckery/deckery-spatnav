"""Run in the deckery distrobox (tools/run_nav.sh does that from anywhere):
    python3 -m deckery_nav [--ensure-analog-export] [--presets FILE]
                           [--makima-state FILE] [--no-pointer]"""

import argparse
import os
import sys

_LAYER_SHELL_PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if _LAYER_SHELL_PRELOAD not in os.environ.get("LD_PRELOAD", "").split(":"):
    existing = os.environ.get("LD_PRELOAD", "")
    os.environ["LD_PRELOAD"] = f"{_LAYER_SHELL_PRELOAD}:{existing}" if existing else _LAYER_SHELL_PRELOAD
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(sys.executable, [sys.executable, "-m", "deckery_nav"] + sys.argv[1:])

from .makima_input import STATE_PATH  # noqa: E402
from .presets import DEFAULT_PATH  # noqa: E402


def main():
    parser = argparse.ArgumentParser(prog="deckery_nav")
    parser.add_argument("--bus-name", default="org.deckery.Atspi")
    parser.add_argument("--presets", default=DEFAULT_PATH)
    parser.add_argument("--makima-state", default=STATE_PATH,
                        help="makima's state file (a fake one for tests)")
    parser.add_argument("--ensure-analog-export", action="store_true",
                        help="TEMPORARY, testing only: switch makima's analog export on when it is off "
                             "(≤ 1/s), off again on exit")
    parser.add_argument("--no-pointer", action="store_true", help="only highlight, never move the pointer")
    args = parser.parse_args()
    from .app import NavApp
    return NavApp(args).run([])


if __name__ == "__main__":
    sys.exit(main())
