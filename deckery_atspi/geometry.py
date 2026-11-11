"""Rectangles and rectangle-list regions in logical screen pixels.

KWin reports fractional logical coordinates (e.g. width 570.909 at scale 1.1),
so everything here is float. A region is a list of non-overlapping Rects —
small enough (a handful of windows) that no spatial index is needed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    @property
    def area(self):
        return self.w * self.h

    @property
    def empty(self):
        return self.w <= 0 or self.h <= 0

    @property
    def center(self):
        return (self.x + self.w / 2, self.y + self.h / 2)

    def intersect(self, other):
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def subtract(self, other):
        """self minus other as up to 4 disjoint rects (top/bottom full-width
        bands, left/right bands in between)."""
        cut = self.intersect(other)
        if cut is None:
            return [self]
        pieces = []
        if cut.y > self.y:
            pieces.append(Rect(self.x, self.y, self.w, cut.y - self.y))
        if cut.y2 < self.y2:
            pieces.append(Rect(self.x, cut.y2, self.w, self.y2 - cut.y2))
        if cut.x > self.x:
            pieces.append(Rect(self.x, cut.y, cut.x - self.x, cut.h))
        if cut.x2 < self.x2:
            pieces.append(Rect(cut.x2, cut.y, self.x2 - cut.x2, cut.h))
        return pieces

    @classmethod
    def from_json(cls, d):
        if not d:
            return None
        return cls(float(d["x"]), float(d["y"]), float(d.get("w", d.get("width", 0))),
                   float(d.get("h", d.get("height", 0))))


def region_subtract(region, rect):
    out = []
    for piece in region:
        out.extend(piece.subtract(rect))
    return out


def region_intersect(region, rect):
    out = []
    for piece in region:
        cut = piece.intersect(rect)
        if cut is not None:
            out.append(cut)
    return out


def region_area(region):
    return sum(r.area for r in region)
