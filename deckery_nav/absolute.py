"""Absolute navigation ("Tastkarte"): pad position → screen → target (pure).

The pad maps onto an area of the screen; the finger point selects the target
under or near it, with hysteresis so the selection does not flicker on
borders. Area, dead border, snap distance, hysteresis, dwell and smoothing are
preset parameters (presets.toml [absolute.*])."""

from dataclasses import dataclass, field

from deckery_atspi.geometry import Rect

from .targets import bounding_box, contains, nearest, target_distance


@dataclass(frozen=True)
class AbsoluteParams:
    area: str = "bildschirm"   # bildschirm | monitor | ziele | fenster
    edge: float = 0.05         # pad border (fraction per side) that is all "edge of the area"
    edge_left: float = -1.0    # per-side overrides (< 0 = use edge); the thumb's contact
    edge_right: float = -1.0   # point reaches some pad edges less far than others —
    edge_top: float = -1.0     # measure with python3 -m deckery_nav.padrange
    edge_bottom: float = -1.0
    invert_y: bool = False     # makima reports pad y up = positive; True flips that
    snap_px: float = 80.0      # finger farther than this from every target: nothing selected
    keep_px: float = 10.0      # current selection holds while the finger is this close to it
    dwell_ms: float = 0.0      # a new candidate must stay best this long before it wins
    smoothing: float = 0.0     # 0 = raw finger point, towards 1 = heavier low-pass
    pointer: str = "ziel"      # ziel (jump to target) | finger (follow finger) | aus


def pad_to_area(px, py, area, params=AbsoluteParams()):
    """Pad coordinates (-1..1, y up unless invert_y) → point in `area` (Rect)."""
    u = (px + 1.0) / 2.0
    v = (1.0 - py) / 2.0 if not params.invert_y else (py + 1.0) / 2.0
    left, right, top, bottom = edges(params)
    u = min(1.0, max(0.0, (u - left) / max(1e-6, 1.0 - left - right)))
    v = min(1.0, max(0.0, (v - top) / max(1e-6, 1.0 - top - bottom)))
    return area.x + u * area.w, area.y + v * area.h


def edges(params):
    """(left, right, top, bottom) dead borders as fractions of the pad, in
    screen orientation."""
    return tuple(e if e >= 0 else params.edge
                 for e in (params.edge_left, params.edge_right, params.edge_top, params.edge_bottom))


def edges_from_range(x_min, x_max, y_min, y_max, tolerance=0.03):
    """Measured pad extremes (makima units, y up) → per-side borders so the
    farthest reachable point plus `tolerance` already hits the screen edge."""
    left = (x_min + 1.0) / 2.0 + tolerance
    right = (1.0 - x_max) / 2.0 + tolerance
    top = (1.0 - y_max) / 2.0 + tolerance
    bottom = (y_min + 1.0) / 2.0 + tolerance
    return tuple(round(max(0.0, e), 3) for e in (left, right, top, bottom))


def choose_area(kind, monitors, targets, selection):
    """The screen rect the pad maps onto. monitors: list of Rect; selection:
    the current Target or None. Unknown inputs fall back to all monitors."""
    screen = bounding_box(monitors) or Rect(0, 0, 1, 1)
    if kind == "monitor":
        if selection is not None:
            sx, sy = selection.point
            for m in monitors:
                if m.x <= sx < m.x2 and m.y <= sy < m.y2:
                    return m
        return monitors[0] if monitors else screen
    if kind == "ziele":
        return bounding_box(v for t in targets for v in t.visible) or screen
    if kind == "fenster":
        if selection is not None:
            box = bounding_box(v for t in targets if t.window == selection.window for v in t.visible)
            if box is not None:
                return box
        return screen
    return screen


@dataclass
class AbsolutePicker:
    params: AbsoluteParams = field(default_factory=AbsoluteParams)
    current: object = None          # Target
    pending: object = None          # Target waiting out dwell_ms
    pending_since: float = 0.0
    smoothed: tuple = None
    landed: bool = False            # first reading of this touch: no dwell

    def lift(self):
        self.pending = None
        self.smoothed = None
        self.landed = False

    def finger(self, x, y):
        a = self.params.smoothing
        if self.smoothed is None or a <= 0:
            self.smoothed = (x, y)
        else:
            sx, sy = self.smoothed
            self.smoothed = (sx * a + x * (1 - a), sy * a + y * (1 - a))
        return self.smoothed

    def update(self, targets, x, y, now):
        """Finger at screen point (x, y) at time `now` (s) → selected Target or None."""
        p = self.params
        first, self.landed = not self.landed, True
        by_id = {t.id: t for t in targets}
        cur = by_id.get(self.current.id) if self.current is not None else None
        cand = nearest(targets, x, y)
        if cand is not None and target_distance(cand, x, y) > p.snap_px:
            cand = None
        if cur is not None and target_distance(cur, x, y) <= p.keep_px:
            # Hysteresis — except for a target nested inside the current one
            # that the finger is actually on (a button inside a list row).
            nested = (cand is not None and cand.id != cur.id and contains(cand, x, y)
                      and cand.main.area < cur.main.area and cur.main.intersect(cand.main) == cand.main)
            if not nested:
                self.pending = None
                self.current = cur
                return cur
        if cand is None or (cur is not None and cand.id == cur.id):
            self.pending = None
            self.current = cand     # empty space selects nothing
            return cand
        if p.dwell_ms > 0 and not first:
            if self.pending is None or self.pending.id != cand.id:
                self.pending, self.pending_since = cand, now
                self.current = cur
                return cur
            if (now - self.pending_since) * 1000.0 < p.dwell_ms:
                self.current = cur
                return cur
        self.pending = None
        self.current = cand
        return cand
