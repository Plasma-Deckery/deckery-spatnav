"""The backend's published elements as navigation targets (pure).

The backend publishes everything visible (BACKEND_V2_DESIGN.md §5); what is
too small to aim at is the navigation's call (§11.3), so the minimum size is
a preset parameter here, not a backend rule."""

import math
from dataclasses import dataclass

from deckery_atspi.geometry import Rect


@dataclass(frozen=True)
class Target:
    id: str
    window: str
    role: str
    name: str
    rect: Rect           # unclipped
    visible: tuple       # visible parts
    point: tuple         # where the pointer goes

    @property
    def main(self):
        """The largest visible part — what the eye takes as the element."""
        return max(self.visible, key=lambda r: r.area)


def targets_from_json(elements, min_size=0.0):
    out = []
    for e in elements:
        visible = tuple(Rect(*v) for v in e["visible"])
        if not visible:
            continue
        if min_size and all(v.w < min_size or v.h < min_size for v in visible):
            continue
        out.append(Target(e["id"], e["window"], e["role"], e["name"], Rect(*e["rect"]),
                          visible, tuple(e["target"])))
    return out


def rect_distance(rect, x, y):
    """0 inside, else Euclidean distance to the rect's border."""
    dx = max(rect.x - x, 0.0, x - rect.x2)
    dy = max(rect.y - y, 0.0, y - rect.y2)
    return math.hypot(dx, dy)


def target_distance(target, x, y):
    return min(rect_distance(v, x, y) for v in target.visible)


def contains(target, x, y):
    return any(v.x <= x < v.x2 and v.y <= y < v.y2 for v in target.visible)


def bounding_box(rects):
    rects = list(rects)
    if not rects:
        return None
    x1 = min(r.x for r in rects)
    y1 = min(r.y for r in rects)
    x2 = max(r.x2 for r in rects)
    y2 = max(r.y2 for r in rects)
    return Rect(x1, y1, x2 - x1, y2 - y1)


def nearest(targets, x, y):
    """Target under or closest to (x, y); among several under it the smallest
    (a button inside a list item, not the list item)."""
    best, best_key = None, None
    for t in targets:
        d = target_distance(t, x, y)
        key = (d, t.main.area if d == 0 else 0.0)
        if best_key is None or key < best_key:
            best, best_key = t, key
    return best
