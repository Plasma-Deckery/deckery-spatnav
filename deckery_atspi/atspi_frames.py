"""Top-level AT-SPI frames: the cheap per-frame facts needed to match them
to KWin windows (matching.py). No tree walking here."""

from dataclasses import dataclass

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from vendor.orca_ax.ax_object import AXObject


def element_id(acc):
    """bus name + object path: stable for the object's lifetime, and read
    from libatspi's local struct (no D-Bus round trip)."""
    return f"{acc.app.bus_name}{acc.path}" if acc.app else acc.path


@dataclass
class FrameInfo:
    id: str
    app_name: str
    pid: int
    toolkit: str
    role: str
    name: str
    width: int
    height: int
    showing: bool
    active: bool
    accessible: object = None

    def to_json(self):
        return {k: v for k, v in self.__dict__.items() if k != "accessible"}


POPUP_ROLES = frozenset(("popup menu", "menu", "window"))


def frame_info(acc):
    """FrameInfo for a single top-level accessible — used for popups, which
    Qt does not list as children of their application (Dolphin's context
    menu: parent=application, index_in_parent=-1) and which can therefore
    only be learned about from their object:state-changed:showing event."""
    app = acc.get_application()
    ext = acc.get_extents(Atspi.CoordType.WINDOW)
    states = acc.get_state_set()
    return FrameInfo(
        id=element_id(acc), app_name=AXObject.get_name(app) or "", pid=app.get_process_id(),
        toolkit=AXObject.get_toolkit_name(app) or "", role=AXObject.get_role_name(acc) or "",
        name=AXObject.get_name(acc) or "", width=ext.width, height=ext.height,
        showing=states.contains(Atspi.StateType.SHOWING),
        active=states.contains(Atspi.StateType.ACTIVE), accessible=acc)


def list_frames():
    frames = []
    desktop = Atspi.get_desktop(0)
    for app in AXObject.iter_children(desktop):
        try:
            pid = app.get_process_id()
        except Exception:
            continue
        app_name = AXObject.get_name(app) or ""
        toolkit = AXObject.get_toolkit_name(app) or ""
        for frame in AXObject.iter_children(app):
            try:
                ext = frame.get_extents(Atspi.CoordType.WINDOW)
                states = frame.get_state_set()
            except Exception:
                continue
            frames.append(FrameInfo(
                id=element_id(frame), app_name=app_name, pid=pid, toolkit=toolkit,
                role=AXObject.get_role_name(frame) or "", name=AXObject.get_name(frame) or "",
                width=ext.width, height=ext.height,
                showing=states.contains(Atspi.StateType.SHOWING),
                active=states.contains(Atspi.StateType.ACTIVE),
                accessible=frame))
    return frames
