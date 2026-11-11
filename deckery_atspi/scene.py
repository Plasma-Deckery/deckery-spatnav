"""The compositor's view of the screen, parsed from one KWin tracker payload.

KWin is the only source of truth for what is on screen: which windows exist,
their z-order, geometry, desktop and output. AT-SPI is only asked about the
contents of windows this module says are on screen (BACKEND_V2_DESIGN.md §3)."""

from dataclasses import dataclass, field

from .geometry import Rect

# KWin's Layer enum (kwin/src/utils/common.h). Only the values this module
# branches on are named.
LAYER_DESKTOP = 0
LAYER_OVERLAY = 9


@dataclass(frozen=True)
class Output:
    name: str
    geometry: Rect
    scale: float


@dataclass(frozen=True)
class Window:
    id: str
    pid: int
    caption: str
    resource_class: str
    layer: int
    output: str
    frame: Rect
    client: Rect
    buffer: Rect
    desktops: tuple
    on_all_desktops: bool
    minimized: bool
    hidden: bool
    flags: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class Scene:
    current_desktop: str
    active_window_id: str
    outputs: tuple
    windows: tuple  # bottom → top, KWin's stacking order

    def on_current_desktop(self, window):
        return window.on_all_desktops or self.current_desktop in window.desktops

    def scale_for(self, window):
        for output in self.outputs:
            if output.name == window.output:
                return output.scale
        return self.outputs[0].scale if self.outputs else 1.0

    def is_mapped(self, window):
        """On screen as far as the compositor is concerned, ignoring
        occlusion: right desktop, not minimized, not a tucked-away auto-hide
        panel."""
        return (self.on_current_desktop(window) and not window.minimized
                and not window.hidden and window.frame is not None
                and not window.frame.empty)


def _window_from_json(d):
    frame = Rect.from_json(d.get("frame"))
    return Window(
        id=d.get("id") or "",
        pid=d.get("pid") or 0,
        caption=d.get("caption") or "",
        resource_class=d.get("resourceClass") or "",
        layer=d.get("layer", -1),
        output=d.get("output") or "",
        frame=frame,
        client=Rect.from_json(d.get("client")) or frame,
        buffer=Rect.from_json(d.get("buffer")) or frame,
        desktops=tuple(d.get("desktops") or ()),
        on_all_desktops=bool(d.get("onAllDesktops")),
        minimized=bool(d.get("minimized")),
        hidden=bool(d.get("hidden")),
        flags=frozenset(d.get("flags") or ()),
    )


def scene_from_payload(payload):
    """Parses the v2 tracker payload (kwin/deckery-windowtracker, version 2):
    `windows` is already in KWin's stacking order, bottom first."""
    outputs = tuple(
        Output(o["name"], Rect.from_json(o["geometry"]), float(o.get("scale", 1.0)))
        for o in payload.get("outputs") or ()
    )
    return Scene(
        current_desktop=payload.get("currentDesktop") or "",
        active_window_id=payload.get("activeWindow") or "",
        outputs=outputs,
        windows=tuple(_window_from_json(w) for w in payload.get("windows") or ()),
    )
