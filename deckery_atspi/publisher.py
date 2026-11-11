"""Publishes the current target list on D-Bus.

Object /Elements, interface org.deckery.Atspi.Elements:
  Get() -> (u version, s json)       full snapshot, for (re)starting consumers
  signal Changed(u version, s json)  full snapshot after every change

json is a list of compose.Element.to_json() objects, screen coordinates in
logical pixels. Consumers refer to elements only by their "id"."""

import json

from gi.repository import GLib

_INTERFACE_XML = """
<node>
  <interface name="org.deckery.Atspi.Elements">
    <method name="Get">
      <arg type="u" name="version" direction="out"/>
      <arg type="s" name="elements" direction="out"/>
    </method>
    <signal name="Changed">
      <arg type="u" name="version"/>
      <arg type="s" name="elements"/>
    </signal>
  </interface>
</node>
"""

INTERFACE = "org.deckery.Atspi.Elements"
PATH = "/Elements"


class ElementPublisher:
    def __init__(self):
        self._connection = None
        self._version = 0
        self._json = "[]"

    def attach(self, connection):
        from gi.repository import Gio
        self._connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(_INTERFACE_XML)
        connection.register_object(PATH, node.interfaces[0], self._on_call, None, None)

    def publish(self, elements):
        text = json.dumps([e.to_json() for e in elements])
        if text == self._json:
            return
        self._json = text
        self._version += 1
        if self._connection is not None:
            self._connection.emit_signal(None, PATH, INTERFACE, "Changed",
                                         GLib.Variant("(us)", (self._version, self._json)))

    def _on_call(self, connection, sender, path, interface, method, params, invocation):
        invocation.return_value(GLib.Variant("(us)", (self._version, self._json)))
