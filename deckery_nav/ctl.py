"""Control a running navigation prototype.

    python3 -m deckery_nav.ctl state
    python3 -m deckery_nav.ctl list
    python3 -m deckery_nav.ctl next relative|absolute     (prev …)
    python3 -m deckery_nav.ctl set relative|absolute NAME

Needs only Gio — runs on the host as well as in the box."""

import json
import sys

from gi.repository import Gio, GLib

NAME, PATH, IFACE = "org.deckery.Nav", "/Nav", "org.deckery.Nav"


def call(method, args=None, out="(s)"):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    reply = bus.call_sync(NAME, PATH, IFACE, method, args, GLib.VariantType(out),
                          Gio.DBusCallFlags.NONE, 2000, None)
    return reply.unpack()[0]


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "state":
        print(json.dumps(json.loads(call("State")), indent=1, ensure_ascii=False))
    elif cmd == "list":
        for kind, items in json.loads(call("List")).items():
            print(kind)
            for p in items:
                print(f"  {p['name']:20} {p['label']}")
    elif cmd in ("next", "prev") and len(argv) == 2:
        print(call("Step", GLib.Variant("(si)", (argv[1], 1 if cmd == "next" else -1))))
    elif cmd == "set" and len(argv) == 3:
        if not call("Set", GLib.Variant("(ss)", (argv[1], argv[2])), "(b)"):
            print(f"no {argv[1]} preset {argv[2]!r}", file=sys.stderr)
            return 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
