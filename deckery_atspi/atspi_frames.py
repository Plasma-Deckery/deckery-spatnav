"""Top-level AT-SPI frames: the cheap per-frame facts needed to match them
to KWin windows (matching.py). No tree walking here."""

import time
from dataclasses import dataclass

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from vendor.orca_ax.ax_object import AXObject

from .atspi_bus import ACCESSIBLE, PROPERTIES, shared_bus

# Registered apps that never answer: libatspi waits up to 800 ms per call
# on them, several calls per app (real desktop 2026-11-12:
# ibus-extension-gtk3 ~20 s, a Python script with a blocked main loop ~6 s
# per frame list — the whole backend stood still meanwhile). All apps are
# asked for their child count at once with this timeout first; whoever
# misses it is not asked again for UNRESPONSIVE_RETRY_S, and keeps the
# frames it had before (a busy browser is no hung input method).
PING_TIMEOUT_MS = 500
UNRESPONSIVE_RETRY_S = 30.0


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
    toolkit_version: str
    role: str
    name: str
    width: int
    height: int
    showing: bool
    active: bool
    accessible: object = None
    # A popup that is a subtree of its main window's frame, in that
    # window's coordinates (backend._match_embedded_popups).
    embedded: bool = False

    def to_json(self):
        return {k: v for k, v in self.__dict__.items() if k != "accessible"}


POPUP_ROLES = frozenset(("popup menu", "menu", "window"))


def _toolkit_version(app):
    try:
        return app.get_toolkit_version() or ""
    except Exception:
        return ""


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
        toolkit=AXObject.get_toolkit_name(app) or "", toolkit_version=_toolkit_version(app),
        role=AXObject.get_role_name(acc) or "",
        name=AXObject.get_name(acc) or "", width=ext.width, height=ext.height,
        showing=states.contains(Atspi.StateType.SHOWING),
        active=states.contains(Atspi.StateType.ACTIVE), accessible=acc)


def _split_id(eid):
    bus_name, _, path = eid.partition("/")
    return bus_name, "/" + path


def _apps_with_frames(apps, unresponsive):
    """The apps worth asking via libatspi: those that answered the child
    count ping in time with at least one child. unresponsive (bus name →
    monotonic time it was found so) is updated in place."""
    bus = shared_bus()
    if bus is None:
        return apps
    now = time.monotonic()
    for bus_name, since in list(unresponsive.items()):
        if now - since > UNRESPONSIVE_RETRY_S:
            del unresponsive[bus_name]
    counts = {}
    with bus.batch() as batch:
        for i, app in enumerate(apps):
            bus_name, path = _split_id(element_id(app))
            if bus_name in unresponsive:
                continue

            def on_reply(reply, i=i):
                counts[i] = reply[0] if reply else None
            batch.call(bus_name, path, PROPERTIES, "Get", GLib.Variant("(ss)", (ACCESSIBLE, "ChildCount")),
                       on_reply, timeout_ms=PING_TIMEOUT_MS)
        batch.run()
    out = []
    for i, app in enumerate(apps):
        if i not in counts:
            continue
        if counts[i] is None:
            unresponsive[_split_id(element_id(app))[0]] = now
        elif counts[i] > 0:
            out.append(app)
    return out


def list_frames(unresponsive=None, previous=()):
    """unresponsive: see _apps_with_frames (None: ask every app via
    libatspi). previous: the last list, for apps not answering now."""
    frames = []
    desktop = Atspi.get_desktop(0)
    apps = list(AXObject.iter_children(desktop))
    if unresponsive is not None:
        apps = _apps_with_frames(apps, unresponsive)
        frames = [f for f in previous if _split_id(f.id)[0] in unresponsive]
    for app in apps:
        try:
            pid = app.get_process_id()
        except Exception:
            continue
        app_name = AXObject.get_name(app) or ""
        toolkit = AXObject.get_toolkit_name(app) or ""
        toolkit_version = _toolkit_version(app)
        for frame in AXObject.iter_children(app):
            try:
                ext = frame.get_extents(Atspi.CoordType.WINDOW)
                states = frame.get_state_set()
            except Exception:
                continue
            frames.append(FrameInfo(
                id=element_id(frame), app_name=app_name, pid=pid, toolkit=toolkit,
                toolkit_version=toolkit_version,
                role=AXObject.get_role_name(frame) or "", name=AXObject.get_name(frame) or "",
                width=ext.width, height=ext.height,
                showing=states.contains(Atspi.StateType.SHOWING),
                active=states.contains(Atspi.StateType.ACTIVE),
                accessible=frame))
    return frames
