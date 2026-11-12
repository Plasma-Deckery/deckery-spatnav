"""Relative navigation: the next target from an origin in a direction (pure).

Three scorers, each a preset choice (presets.toml [relative.*] scorer = …):

- "kegel" (cone): centre-to-centre vector within a cone around the
  direction; score = distance, penalised by the angle off the direction.
- "kante" (edge, the browser/W3C spatial-navigation idea): rects projected
  onto the direction axis; the gap from the origin's leading edge to the
  candidate's near edge, plus the sideways gap weighted. Rows and columns
  win over diagonals, adjacent beats far. A free angle within 22.5° of an
  axis counts as that axis; diagonals take the nearest target past the
  origin's centre on both axes.
- "strahl" (ray): a beam of given width from the origin centre; the first
  target it hits. Nothing hit → falls back to "kegel".

Direction is an angle in radians in screen space: 0 = right, π/2 = down."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DirectionParams:
    scorer: str = "kante"
    cone_deg: float = 60.0        # kegel: half-angle of the cone
    angle_weight: float = 2.0     # kegel: penalty factor at the cone's edge
    side_weight: float = 3.0      # kante: sideways gap counts this much more
    beam_px: float = 24.0         # strahl: beam width


def _unit(angle):
    return math.cos(angle), math.sin(angle)


def _project(rect, ux, uy):
    """The rect's extent along axis (ux, uy) as (lo, hi)."""
    xs = (rect.x * ux, rect.x2 * ux)
    ys = (rect.y * uy, rect.y2 * uy)
    return min(xs) + min(ys), max(xs) + max(ys)


def _candidates(origin, targets):
    """Everything but the origin and what shares its spot (panel buttons
    nested twice with the same rect, a list item around its label)."""
    ox, oy = origin.main.center
    for t in targets:
        if t.id == origin.id:
            continue
        m = t.main
        if m.x <= ox < m.x2 and m.y <= oy < m.y2:
            continue
        yield t


def _score_kegel(origin, t, ux, uy, p):
    ox, oy = origin.main.center
    cx, cy = t.main.center
    vx, vy = cx - ox, cy - oy
    dist = math.hypot(vx, vy)
    if dist == 0:
        return None
    cos = (vx * ux + vy * uy) / dist
    off = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
    if off > p.cone_deg:
        return None
    return dist * (1.0 + p.angle_weight * (off / p.cone_deg) ** 2)


_AHEAD_EPS = 0.5


def _score_kante(origin, t, ux, uy, p):
    angle = math.atan2(uy, ux)
    axis = round(angle / (math.pi / 2)) * (math.pi / 2)
    if abs(angle - axis) > math.radians(22.5):
        return _score_kante_diagonal(origin, t, ux, uy)
    ux, uy = round(math.cos(axis)), round(math.sin(axis))
    # along the direction
    o_lo, o_hi = _project(origin.main, ux, uy)
    c_lo, c_hi = _project(t.main, ux, uy)
    if c_lo < (o_lo + o_hi) / 2 + _AHEAD_EPS:
        return None     # starts before the origin's middle: beside or behind, not ahead
    gap = max(0.0, c_lo - o_hi)
    # sideways
    vx, vy = -uy, ux
    s_lo, s_hi = _project(origin.main, vx, vy)
    t_lo, t_hi = _project(t.main, vx, vy)
    side = max(0.0, t_lo - s_hi, s_lo - t_hi)
    # Tie-break inside a row: closer centres first.
    ocx, ocy = origin.main.center
    tcx, tcy = t.main.center
    centre_side = abs((tcx - ocx) * vx + (tcy - ocy) * vy)
    return gap + p.side_weight * side + 0.01 * centre_side


def _score_kante_diagonal(origin, t, ux, uy):
    """Diagonals: the candidate lies past the origin's centre on both axes;
    nearest by rect gap, angle off the diagonal as penalty."""
    ocx, ocy = origin.main.center
    m = t.main
    if (m.x < ocx) if ux > 0 else (m.x2 > ocx):
        return None
    if (m.y < ocy) if uy > 0 else (m.y2 > ocy):
        return None
    o = origin.main
    gx = max(0.0, m.x - o.x2, o.x - m.x2)
    gy = max(0.0, m.y - o.y2, o.y - m.y2)
    cx, cy = m.center
    dist = math.hypot(cx - ocx, cy - ocy)
    off = math.acos(max(-1.0, min(1.0, ((cx - ocx) * ux + (cy - ocy) * uy) / dist)))
    return math.hypot(gx, gy) * (1.0 + (off / (math.pi / 4)) ** 2) + 0.01 * dist


def _ray_hit(ox, oy, ux, uy, rect, half):
    """Distance along the ray at which a beam of half-width `half` first
    touches the rect, or None."""
    r_lo, r_hi = _project(rect, ux, uy)
    s_lo, s_hi = _project(rect, -uy, ux)
    o_along = ox * ux + oy * uy
    o_side = -ox * uy + oy * ux
    if s_hi < o_side - half or s_lo > o_side + half:
        return None
    if r_hi <= o_along:
        return None
    return max(0.0, r_lo - o_along)


def pick(origin, targets, angle, params=DirectionParams()):
    """The best target from `origin` towards `angle`, or None."""
    ux, uy = _unit(angle)
    cands = list(_candidates(origin, targets))
    if params.scorer == "strahl":
        ox, oy = origin.main.center
        hits = [(d, t.main.area, t) for t in cands
                if (d := _ray_hit(ox, oy, ux, uy, t.main, params.beam_px / 2)) is not None]
        if hits:
            return min(hits, key=lambda h: (h[0], h[1]))[2]
        scorer = _score_kegel
    elif params.scorer == "kegel":
        scorer = _score_kegel
    elif params.scorer == "kante":
        scorer = _score_kante
    else:
        raise ValueError(f"unknown scorer {params.scorer!r}")
    best, best_score = None, None
    for t in cands:
        s = scorer(origin, t, ux, uy, params)
        if s is not None and (best_score is None or s < best_score):
            best, best_score = t, s
    return best


def quantize(angle, directions):
    """Snap a stick angle to 4 or 8 directions; 0 (or anything else) = free."""
    if directions in (4, 8):
        step = 2 * math.pi / directions
        return round(angle / step) * step
    return angle
