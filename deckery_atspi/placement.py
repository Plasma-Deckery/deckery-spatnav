"""Window-relative AT-SPI rects → screen rects, plus clipping (pure).

One resolver with named rules instead of the old fallback cascade
(BACKEND_V2_DESIGN.md §6):

- origin: KWin bufferGeometry. AT-SPI's WINDOW coordinates are anchored at
  the surface buffer's top-left. For server-side-decorated apps buffer ==
  client; for client-side-decorated ones (GTK, Gecko, Chromium) the buffer
  includes the shadow margin (deckery#28: ~26/23 px for Firefox).
- rule "chromium-dip": Chromium's web contents report CSS/DIP pixels, i.e.
  output scale too large; its own titlebar buttons do not (deckery#28).
  Applied with the window's own output scale, no longer hardcoded 1.1."""

from dataclasses import dataclass

from .geometry import Rect, region_intersect

CHROMIUM_TITLEBAR_BUTTONS = frozenset(("Minimize", "Maximize", "Restore", "Close"))


@dataclass(frozen=True)
class Placement:
    origin_x: float
    origin_y: float
    dip_scale: float  # 1.0 unless rule chromium-dip applies

    def to_screen(self, rect, name=""):
        scale = 1.0 if name in CHROMIUM_TITLEBAR_BUTTONS else self.dip_scale
        return Rect(rect.x / scale + self.origin_x, rect.y / scale + self.origin_y,
                    rect.w / scale, rect.h / scale)


def placement_for(scene, window, toolkit):
    dip = scene.scale_for(window) if (toolkit or "").lower() == "chromium" else 1.0
    origin = window.buffer or window.frame
    return Placement(origin.x, origin.y, dip)


def clip(rect, ancestor_clips, region):
    """Visible parts of a screen rect: inside every clipping ancestor and
    inside the window's visible region. Empty list = not a target."""
    for ancestor in ancestor_clips:
        rect = rect.intersect(ancestor)
        if rect is None:
            return []
    return region_intersect(region, rect)


def target_point(parts):
    """Center of the largest visible part."""
    best = max(parts, key=lambda r: r.area)
    return best.center
