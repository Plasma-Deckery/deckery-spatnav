"""Receives the scene from KWin.

Owns the backend's D-Bus name, loads kwin_scene.js into KWin via /Scripting
once the name is ours (so KWin's first push can't go to a name owned by
someone else), and hands each parsed Scene to `on_scene`. Bursts of pushes
(window drags emit frameGeometryChanged per frame) are coalesced into one
callback per main-loop iteration — only the latest payload matters."""

import json
import os

from gi.repository import Gio, GLib

from .scene import scene_from_payload

SCRIPT_PLUGIN_NAME = "deckery-atspi-scene"
_SCRIPT_TEMPLATE = os.path.join(os.path.dirname(__file__), "kwin_scene.js")

_INTERFACE_XML = """
<node>
  <interface name="org.deckery.Atspi.Scene">
    <method name="Update"><arg type="s" name="payload" direction="in"/></method>
  </interface>
</node>
"""


class KWinSceneSource:
    def __init__(self, bus_name, on_scene, log=print, on_bus_acquired=None):
        self.bus_name = bus_name
        self._on_bus_acquired_hook = on_bus_acquired
        self._on_scene = on_scene
        self._log = log
        self._connection = None
        self._owner_id = 0
        self._pending_payload = None
        self._idle_id = 0
        self.last_payload = None

    def start(self):
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, self.bus_name, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, self._on_name_acquired, self._on_name_lost)

    def stop(self):
        if self._connection is not None:
            self._unload_script(self._connection)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0

    def _on_bus_acquired(self, connection, name):
        self._connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(_INTERFACE_XML)
        connection.register_object("/Scene", node.interfaces[0], self._on_method_call, None, None)
        if self._on_bus_acquired_hook:
            self._on_bus_acquired_hook(connection)

    def _on_name_acquired(self, connection, name):
        self._log(f"[kwin] owning {name}, loading scene script")
        self._unload_script(connection)  # leftover from a crashed previous run
        script_path = self._render_script()
        try:
            (script_id,) = connection.call_sync(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                GLib.Variant("(ss)", (script_path, SCRIPT_PLUGIN_NAME)),
                GLib.VariantType("(i)"), Gio.DBusCallFlags.NONE, 2000, None).unpack()
            connection.call_sync(
                "org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script", "run",
                None, None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            self._log(f"[kwin] could not load scene script: {e}")

    def _on_name_lost(self, connection, name):
        self._log(f"[kwin] could not own {name} (another instance running?)")

    def _render_script(self):
        with open(_SCRIPT_TEMPLATE) as f:
            source = f.read().replace("__BUS_NAME__", self.bus_name)
        path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"{self.bus_name}.kwin_scene.js")
        with open(path, "w") as f:
            f.write(source)
        return path

    def _unload_script(self, connection):
        try:
            connection.call_sync(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                GLib.Variant("(s)", (SCRIPT_PLUGIN_NAME,)), GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            pass

    def _on_method_call(self, connection, sender, path, interface, method, params, invocation):
        self._pending_payload = params.unpack()[0]
        invocation.return_value(None)
        if not self._idle_id:
            self._idle_id = GLib.idle_add(self._flush)

    def _flush(self):
        self._idle_id = 0
        raw, self._pending_payload = self._pending_payload, None
        try:
            payload = json.loads(raw)
        except ValueError as e:
            self._log(f"[kwin] bad payload: {e}")
            return GLib.SOURCE_REMOVE
        self.last_payload = payload
        self._on_scene(scene_from_payload(payload))
        return GLib.SOURCE_REMOVE
