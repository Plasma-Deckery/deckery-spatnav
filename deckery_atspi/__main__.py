"""Entry point: python3 -m deckery_atspi [--bus-name NAME] [--dump-scene PATH] [--once]

Single process, single thread, default GLib main context
(BACKEND_V2_DESIGN.md §3). Run inside the deckery distrobox."""

import argparse
import json
import signal
import sys
import time

from gi.repository import GLib

from deckery_a11y import enable_chromium_accessibility

from .backend import Backend
from .kwin_source import KWinSceneSource
from .publisher import ElementPublisher

DEFAULT_BUS_NAME = "org.deckery.Atspi"


def _ts():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def log(msg):
    print(f"[{_ts()}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus-name", default=DEFAULT_BUS_NAME)
    parser.add_argument("--dump-scene", help="append every raw KWin payload to this JSONL file")
    parser.add_argument("--once", action="store_true", help="read all visible windows once and exit")
    args = parser.parse_args()

    enable_chromium_accessibility()
    publisher = ElementPublisher()
    backend = Backend(log=log, on_elements=publisher.publish)
    dump = open(args.dump_scene, "a") if args.dump_scene else None
    loop = GLib.MainLoop()
    source = None
    reported_unmatched = set()

    def on_scene(scene):
        if dump:
            dump.write(json.dumps(source.last_payload) + "\n")
            dump.flush()
        backend.on_scene(scene)
        for entry in backend.unmatched_report():
            if entry not in reported_unmatched:
                reported_unmatched.add(entry)
                log(f"[unmatched] {entry[0]} {entry[1][:40]!r}: {entry[2]}")
        if args.once:
            quit_loop()

    def quit_loop(*_):
        source.stop()
        loop.quit()
        return GLib.SOURCE_REMOVE

    source = KWinSceneSource(args.bus_name, on_scene, log=log, on_bus_acquired=publisher.attach)
    source.start()
    if not args.once:
        backend.start_events()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_loop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, quit_loop)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
