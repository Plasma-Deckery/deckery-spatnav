"""Real pointer/keyboard input into the TEST desktop via KWin's EIS (libei).

KWin offers org.kde.KWin.EIS.RemoteDesktop.connectToEIS on the session bus;
the returned fd speaks the EI protocol, driven here through libei with
ctypes (no headers or bindings needed). Only reaches the compositor whose
session bus we talk to — run it through run.sh so that is the test desktop,
never the real one. Run on the HOST (libei lives there):

    tools/testdesk/run.sh python3 tools/testdesk/eis_input.py click 400 300
    ... rightclick X Y | move X Y | scroll X Y CLICKS | key NAME [NAME ...]
    ... combo alt+f   (keys pressed together, released in reverse)

Coordinates are logical screen pixels, same space as the backend's rects."""

import ctypes
import ctypes.util
import os
import select
import sys
import time

from gi.repository import Gio, GLib

# enum ei_device_capability (libei.h)
CAP_POINTER, CAP_POINTER_ABSOLUTE, CAP_KEYBOARD, CAP_TOUCH, CAP_SCROLL, CAP_BUTTON = (1 << i for i in range(6))
# enum ei_event_type (libei.h)
EV_CONNECT, EV_DISCONNECT, EV_SEAT_ADDED, EV_SEAT_REMOVED, EV_DEVICE_ADDED, EV_DEVICE_REMOVED, \
    EV_DEVICE_PAUSED, EV_DEVICE_RESUMED = range(1, 9)

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
KEYS = {
    "esc": 1, "escape": 1, "tab": 15, "enter": 28, "return": 28, "space": 57, "backspace": 14,
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125, "menu": 127,
    "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "delete": 111,
    "f11": 87, "f12": 88,
}
for _i in range(10):
    KEYS[f"f{_i + 1}"] = 59 + _i
for _i, _c in enumerate("1234567890"):
    KEYS[_c] = 2 + _i
for _row, _start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    for _i, _c in enumerate(_row):
        KEYS[_c] = _start + _i

_lib = ctypes.CDLL(ctypes.util.find_library("ei") or "libei.so.1")
_p = ctypes.c_void_p
for name, res, args in (
    ("ei_new_sender", _p, [_p]),
    ("ei_configure_name", None, [_p, ctypes.c_char_p]),
    ("ei_setup_backend_fd", ctypes.c_int, [_p, ctypes.c_int]),
    ("ei_get_fd", ctypes.c_int, [_p]),
    ("ei_dispatch", None, [_p]),
    ("ei_get_event", _p, [_p]),
    ("ei_event_get_type", ctypes.c_int, [_p]),
    ("ei_event_get_seat", _p, [_p]),
    ("ei_event_get_device", _p, [_p]),
    ("ei_event_unref", _p, [_p]),
    ("ei_event_type_to_string", ctypes.c_char_p, [ctypes.c_int]),
    ("ei_device_ref", _p, [_p]),
    ("ei_device_has_capability", ctypes.c_bool, [_p, ctypes.c_int]),
    ("ei_device_start_emulating", None, [_p, ctypes.c_uint32]),
    ("ei_device_stop_emulating", None, [_p]),
    ("ei_device_frame", None, [_p, ctypes.c_uint64]),
    ("ei_device_pointer_motion_absolute", None, [_p, ctypes.c_double, ctypes.c_double]),
    ("ei_device_button_button", None, [_p, ctypes.c_uint32, ctypes.c_bool]),
    ("ei_device_scroll_discrete", None, [_p, ctypes.c_int32, ctypes.c_int32]),
    ("ei_device_keyboard_key", None, [_p, ctypes.c_uint32, ctypes.c_bool]),
    ("ei_now", ctypes.c_uint64, [_p]),
    ("ei_unref", _p, [_p]),
):
    fn = getattr(_lib, name)
    fn.restype = res
    fn.argtypes = args
_lib.ei_seat_bind_capabilities.restype = None  # variadic, NULL-terminated


class EisInput:
    def __init__(self):
        # KWin drops the EIS context as soon as the caller's D-Bus name
        # disappears, and Gio only holds the session-bus singleton weakly:
        # keep a reference for our whole lifetime.
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        # KWin's capability flags for connectToEIS: keyboard 1, pointer 2, touch 4.
        reply, fds = self._bus.call_with_unix_fd_list_sync(
            "org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop", "org.kde.KWin.EIS.RemoteDesktop",
            "connectToEIS", GLib.Variant("(i)", (1 | 2,)), GLib.VariantType("(hi)"),
            Gio.DBusCallFlags.NONE, 5000, None, None)
        fd = fds.get(reply.unpack()[0])
        self._ei = _lib.ei_new_sender(None)
        _lib.ei_configure_name(self._ei, b"deckery-testdesk")
        if _lib.ei_setup_backend_fd(self._ei, fd) != 0:
            raise RuntimeError("ei_setup_backend_fd failed")
        self._sequence = 0
        self.devices = []
        self._resumed = set()
        self._wait(lambda: self._has(CAP_POINTER_ABSOLUTE) and self._has(CAP_KEYBOARD), 5.0)

    def _has(self, cap):
        return any(_lib.ei_device_has_capability(d, cap) and d in self._resumed for d in self.devices)

    def _device(self, cap):
        for d in self.devices:
            if d in self._resumed and _lib.ei_device_has_capability(d, cap):
                return d
        raise RuntimeError(f"no resumed device with capability {cap}")

    def _pump(self, timeout_s):
        fd = _lib.ei_get_fd(self._ei)
        select.select([fd], [], [], timeout_s)
        _lib.ei_dispatch(self._ei)
        while True:
            event = _lib.ei_get_event(self._ei)
            if not event:
                break
            etype = _lib.ei_event_get_type(event)
            if etype == EV_SEAT_ADDED:
                _lib.ei_seat_bind_capabilities(
                    _p(_lib.ei_event_get_seat(event)), ctypes.c_int(CAP_POINTER_ABSOLUTE),
                    ctypes.c_int(CAP_BUTTON), ctypes.c_int(CAP_SCROLL), ctypes.c_int(CAP_KEYBOARD),
                    ctypes.c_int(CAP_POINTER), _p(None))
            elif etype == EV_DEVICE_ADDED:
                self.devices.append(_lib.ei_device_ref(_lib.ei_event_get_device(event)))
            elif etype == EV_DEVICE_RESUMED:
                self._resumed.add(_lib.ei_event_get_device(event))
            elif etype == EV_DEVICE_PAUSED:
                self._resumed.discard(_lib.ei_event_get_device(event))
            elif etype == EV_DISCONNECT:
                raise RuntimeError("EIS disconnected")
            _lib.ei_event_unref(event)

    def _wait(self, condition, timeout_s):
        deadline = time.monotonic() + timeout_s
        while not condition():
            if time.monotonic() > deadline:
                raise RuntimeError("timed out waiting for EIS devices")
            self._pump(0.05)

    def _emit(self, cap, action):
        device = self._device(cap)
        self._sequence += 1
        _lib.ei_device_start_emulating(device, self._sequence)
        action(device)
        _lib.ei_device_frame(device, _lib.ei_now(self._ei))
        _lib.ei_device_stop_emulating(device)
        self._pump(0.03)

    def move(self, x, y):
        self._emit(CAP_POINTER_ABSOLUTE, lambda d: _lib.ei_device_pointer_motion_absolute(d, x, y))

    def button(self, button, press):
        self._emit(CAP_BUTTON, lambda d: _lib.ei_device_button_button(d, button, press))

    def click(self, x, y, button=BTN_LEFT):
        self.move(x, y)
        time.sleep(0.05)
        self.button(button, True)
        time.sleep(0.05)
        self.button(button, False)

    def scroll(self, x, y, clicks):
        self.move(x, y)
        self._emit(CAP_SCROLL, lambda d: _lib.ei_device_scroll_discrete(d, 0, 120 * clicks))

    def key(self, code, press):
        self._emit(CAP_KEYBOARD, lambda d: _lib.ei_device_keyboard_key(d, code, press))

    def tap(self, *names):
        codes = [KEYS[n.lower()] for n in names]
        for c in codes:
            self.key(c, True)
            time.sleep(0.03)
        for c in reversed(codes):
            self.key(c, False)
            time.sleep(0.03)


def main(argv):
    cmd, args = argv[0], argv[1:]
    eis = EisInput()
    if cmd == "click":
        eis.click(float(args[0]), float(args[1]))
    elif cmd == "rightclick":
        eis.click(float(args[0]), float(args[1]), BTN_RIGHT)
    elif cmd == "move":
        eis.move(float(args[0]), float(args[1]))
    elif cmd == "scroll":
        eis.scroll(float(args[0]), float(args[1]), int(args[2]))
    elif cmd == "key":
        for name in args:
            eis.tap(name)
            time.sleep(0.05)
    elif cmd == "combo":
        eis.tap(*args[0].split("+"))
    else:
        print(__doc__)
        return 2
    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
