"""Raw window-relative elements + scene → on-screen targets (pure).

Runs on every scene change (window moved, restacked, desktop switched)
without touching AT-SPI."""

from dataclasses import dataclass

from .geometry import Rect
from .placement import clip, target_point


@dataclass(frozen=True)
class Element:
    id: str
    window_id: str
    role: str
    name: str
    rect: Rect          # screen, unclipped
    visible: tuple      # screen rects actually visible
    target: tuple       # (x, y)
    inferred: bool = False  # geometry reconstructed (menu_repair), not reported

    def to_json(self):
        r = self.rect
        return {"id": self.id, "window": self.window_id, "role": self.role, "name": self.name,
                "rect": [r.x, r.y, r.w, r.h],
                "visible": [[v.x, v.y, v.w, v.h] for v in self.visible],
                "target": list(self.target), **({"inferred": True} if self.inferred else {})}


def compose_window(raw_elements, window_id, placement, region):
    out = []
    for raw in raw_elements:
        screen = placement.to_screen(raw.rect, raw.name)
        clips = tuple(placement.to_screen(c) for c in raw.clips)
        parts = clip(screen, clips, region)
        if parts:
            out.append(Element(raw.id, window_id, raw.role, raw.name, screen,
                               tuple(parts), target_point(parts), raw.inferred))
    return out
