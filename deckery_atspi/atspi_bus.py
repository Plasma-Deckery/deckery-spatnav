"""Pipelined D-Bus calls on the AT-SPI bus, for reading whole subtrees.

libatspi makes one blocking round trip per property (~0.4–0.7 ms each,
measured on the test desktop 2026-11-12), and Qt offers no bulk call
(no usable Cache.GetItems, no Collection). Many calls in flight at once
are much cheaper per call: 200 parallel GetRole calls to Dolphin cost
~0.09 ms each instead of ~0.7 ms one after another.

A private Gio.DBusConnection to the AT-SPI bus with its own
GLib.MainContext: while a Batch runs, that context is pushed as
thread-default (GDBus delivers each reply to the context that was
thread-default when the call was made) and only it is iterated. The
default context — AT-SPI events, KWin pushes — is never iterated from
inside a read, so the caller still sees a plain synchronous function
(BACKEND_V2_DESIGN.md §3: one thread; GDBus' own worker thread already
exists for the session bus and is internal to GLib)."""

import os
import sys

from gi.repository import Gio, GLib

ACCESSIBLE = "org.a11y.atspi.Accessible"
COMPONENT = "org.a11y.atspi.Component"
PROPERTIES = "org.freedesktop.DBus.Properties"
# libatspi's "no object" reference (atspi-constants.h ATSPI_DBUS_PATH_NULL).
NULL_PATH = "/org/a11y/atspi/null"
COORD_TYPE_WINDOW = 1

# Per call. Generous on purpose: a busy app works through the whole queue
# of a batch in order, so the last reply of a large batch can take a while
# (plasmashell right after login). A hung app still costs ~this once per
# batch, not once per call like libatspi's sequential timeouts.
CALL_TIMEOUT_MS = 3000


def _bus_address():
    """Same lookup order as libatspi: AT_SPI_BUS_ADDRESS, then the
    org.a11y.Bus service on the session bus."""
    address = os.environ.get("AT_SPI_BUS_ADDRESS")
    if address:
        return address
    session = Gio.bus_get_sync(Gio.BusType.SESSION)
    (address,) = session.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
                                   None, GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000,
                                   None).unpack()
    return address


class AtspiBus:
    def __init__(self):
        self._context = GLib.MainContext.new()
        self._connection = None

    def _connect(self):
        if self._connection is not None and not self._connection.is_closed():
            return self._connection
        self._connection = None
        # Created with the private context pushed, so the connection's own
        # signals (closed) are dispatched there too, never on the default
        # context. exit-on-close stays off: losing the AT-SPI bus must not
        # end the backend.
        self._context.push_thread_default()
        try:
            self._connection = Gio.DBusConnection.new_for_address_sync(
                _bus_address(),
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        finally:
            self._context.pop_thread_default()
        return self._connection

    def batch(self):
        return Batch(self._context, self._connect())

    def call_sync(self, dest, path, iface, method, args=None):
        """One blocking call (blocks on GDBus' worker thread, iterates no
        main context). Returns the unpacked reply, None on error."""
        try:
            return self._connect().call_sync(dest, path, iface, method, args, None,
                                             Gio.DBusCallFlags.NO_AUTO_START, CALL_TIMEOUT_MS,
                                             None).unpack()
        except GLib.Error:
            return None


class Batch:
    """Calls issued via call() may be answered in any order; their
    callbacks may issue further calls. run() returns once nothing is
    pending any more."""

    def __init__(self, context, connection):
        self._context = context
        self._connection = connection
        self._pending = 0
        self.calls = 0
        self.errors = 0
        self._pushed = False

    def __enter__(self):
        self._context.push_thread_default()
        self._pushed = True
        return self

    def __exit__(self, *exc):
        if self._pushed:
            self._context.pop_thread_default()
            self._pushed = False
        return False

    def call(self, dest, path, iface, method, args, on_reply):
        """on_reply(unpacked reply tuple), or on_reply(None) on any D-Bus
        error (object gone, interface not implemented, timeout)."""
        self._pending += 1
        self.calls += 1
        self._connection.call(dest, path, iface, method, args, None, Gio.DBusCallFlags.NO_AUTO_START,
                              CALL_TIMEOUT_MS, None, self._done, on_reply)

    def _done(self, connection, result, on_reply):
        self._pending -= 1
        try:
            value = connection.call_finish(result).unpack()
        except GLib.Error:
            self.errors += 1
            value = None
        try:
            on_reply(value)
        except Exception:
            # A broken callback must not leave run() waiting forever.
            sys.excepthook(*sys.exc_info())

    def run(self):
        while self._pending:
            self._context.iteration(True)


_shared = None


def shared_bus():
    """The process-wide AtspiBus, or None if the AT-SPI bus can't be
    reached (callers fall back to libatspi)."""
    global _shared
    if _shared is None:
        _shared = AtspiBus()
    try:
        _shared._connect()
    except GLib.Error as error:
        print(f"[atspi_bus] cannot connect to the AT-SPI bus: {error}", file=sys.stderr)
        return None
    return _shared
