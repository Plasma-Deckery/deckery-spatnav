"""Window-relative AT-SPI rects → screen rects, plus clipping (pure).

One resolver with named rules instead of the old fallback cascade
(BACKEND_V2_DESIGN.md §6):

- origin: KWin bufferGeometry. AT-SPI's WINDOW coordinates are anchored at
  the surface buffer's top-left. For server-side-decorated apps buffer ==
  client; for client-side-decorated ones (GTK, Gecko, Chromium) the buffer
  includes the shadow margin (deckery#28: ~26/23 px for Firefox).
- rule "gtk4-client-origin": GTK4 anchors WINDOW coordinates at the
  content (client) origin, not at the buffer with its CSD shadow margin —
  measured on the test desktop 2026-11-11 with Flatseal: buffer origin put
  every box exactly the 25.45 px shadow margin up-left. GTK3/Gecko keep
  the buffer origin (Firefox measurement above).
- rule "chromium-dip": Chromium's web contents (everything inside a
  "document web", reader.py marks it) report CSS/DIP pixels, i.e. output
  scale too large. Chromium's own UI around it does not: deckery#28 found
  that for the titlebar buttons; on the test desktop 2026-11-12 (Chromium
  Flatpak, Wayland, scale 1.1) the tab strip, toolbar and address bar
  behaved the same, each landing exactly when not divided. Applied with
  the window's own output scale, no longer hardcoded 1.1."""

from dataclasses import dataclass

from .geometry import Rect, region_intersect

@dataclass(frozen=True)
class Placement:
    origin_x: float
    origin_y: float
    dip_scale: float  # 1.0 unless rule chromium-dip applies

    def to_screen(self, rect, web=False):
        scale = self.dip_scale if web else 1.0
        return Rect(rect.x / scale + self.origin_x, rect.y / scale + self.origin_y,
                    rect.w / scale, rect.h / scale)


def placement_for(scene, window, toolkit, toolkit_version=""):
    toolkit = (toolkit or "").lower()
    dip = scene.scale_for(window) if toolkit == "chromium" else 1.0
    if toolkit == "gtk" and (toolkit_version or "").split(".")[0].isdigit() \
            and int(toolkit_version.split(".")[0]) >= 4:
        origin = window.client or window.frame
    else:
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
