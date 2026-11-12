"""Jumps the real pointer: absolute motion through KWin's EIS
(org.kde.KWin.EIS.RemoteDesktop, no permission prompt on the real desktop
either). Coordinates are logical screen pixels — the backend's space."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools", "testdesk"))
import eis_input  # noqa: E402


class Pointer:
    def __init__(self):
        self._eis = eis_input.EisInput()
        self.last = None

    def warp(self, x, y):
        if self.last == (x, y):
            return
        lib = eis_input._lib
        self._eis._pump(0)   # KWin swaps the absolute device now and then: be current
        try:
            device = self._eis._device(eis_input.CAP_POINTER_ABSOLUTE)
        except RuntimeError:
            self._eis._pump(0)
            return
        self._eis._sequence += 1
        lib.ei_device_start_emulating(device, self._eis._sequence)
        lib.ei_device_pointer_motion_absolute(device, float(x), float(y))
        lib.ei_device_frame(device, lib.ei_now(self._eis._ei))
        lib.ei_device_stop_emulating(device)
        self.last = (x, y)
        self._eis._pump(0)   # flush without the 30 ms wait EisInput._emit does

    def service(self):
        """Process EIS events (device swaps, pings) while idle."""
        self._eis._pump(0)
