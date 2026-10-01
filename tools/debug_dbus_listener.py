#!/usr/bin/env python3
"""Ad-hoc listener for org.deckery.Debug's Dump(payload_json) — pairs with
tools/kwin_dump_windowlist.js, run via KWin's /Scripting interface, to
capture workspace.windowList() output (KWin script print() didn't surface
anywhere findable, this reuses the already-proven callDBus mechanism from
deckery_tracker.py instead)."""

import json

from gi.repository import GLib, Gio

BUS_NAME = "org.deckery.Debug"
OBJECT_PATH = "/Debug"
INTERFACE_XML = """
<node>
  <interface name="org.deckery.Debug">
    <method name="Dump">
      <arg type="s" name="payload_json" direction="in"/>
    </method>
  </interface>
</node>
"""


def on_method_call(connection, sender, object_path, interface_name, method_name, parameters, invocation):
    (payload_json,) = parameters.unpack()
    data = json.loads(payload_json)
    print(f"=== received {len(data)} window(s) ===")
    for w in data:
        print(f"  {w}")
    invocation.return_value(None)


def on_bus_acquired(connection, name):
    node_info = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
    connection.register_object(OBJECT_PATH, node_info.interfaces[0], on_method_call, None, None)
    print(f"[debug-listener] registered {OBJECT_PATH} on {name}, waiting...")


def on_name_lost(connection, name):
    print(f"[debug-listener] FAILED to own {name}")


Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                  on_bus_acquired, None, on_name_lost)

loop = GLib.MainLoop()
GLib.timeout_add_seconds(30, loop.quit)
loop.run()
