#!/usr/bin/env python3
"""
cursor_nav_strategy.py — pluggable navigation-mapping layer for
cursor_nav_prototype.py.

Deliberately separated out: which model maps stick input to dot
movement/focus is still being iterated on live (see CURSOR_NAV_DESIGN.md).
Three tried so far:

  - FreeMagnetStrategy: dot has a free screen position, moves by a fixed
    step per discrete stick push, magnetically snaps onto a real element
    only when close. Can sit in empty space with nothing focused. Built
    first, then rejected: only real candidate elements should ever be
    navigable/focusable — a floating "nothing focused" state doesn't
    belong in the target interaction.
  - SpringAnchorStrategy (current default): the dot is NEVER free — it is
    always anchored to a real candidate ("sticky" start point) and only
    visually stretches toward the stick's direction like a spring, with
    growing resistance the further it's pulled. The stretch cap is scored
    continuously against the real candidate in that exact direction (not a
    fixed pixel constant) — it's the target's actual distance times
    STRETCH_FRACTION, so the dot sits just short of the midpoint between
    anchor and target right at the commit threshold regardless of how
    close or far apart they are. Past that threshold it re-anchors to the
    target and locks out further stick input (ratchet: "mein Joystick
    macht gar nichts mehr, bis ich ihn nicht wieder in Richtung Mitte
    bewege") until the stick is released back toward center. The actual
    flight to the new anchor after a commit happens in tick(), a
    periodic call independent of stick events, so it keeps animating even
    if the stick is held still right after committing.
  - SemanticGainFieldStrategy: no hops, no ratchet, no raycast. The stick
    drives dot VELOCITY directly (fly it around continuously), but the
    effective speed is scaled by a continuous gain field summed from every
    candidate (adapted from Semantic Pointing, CHI2004) — slow and precise
    near clusters, full speed in open space. No hard boundaries anywhere;
    focus is just "nearest candidate, if close enough" and can legitimately
    be nothing. See its own docstring for the full mechanism/formula.

Keeping this behind one small interface means trying a further model
later is a new class here, not a rewrite of the GTK/AT-SPI/tray plumbing
in cursor_nav_prototype.py.

Interface a strategy must implement:
  initial_state(candidates, screen_center) -> NavState
  update(state, candidates, stick_x, stick_y) -> NavState   [continuous,
      called on every raw stick axis reading — NOT gated by a deadzone in
      the caller; each strategy decides its own response curve/threshold]
  update_after_render(state, candidates) -> NavState   [candidate list
      changed — elements appeared/disappeared — re-validate state]
  tick(state, candidates, dt) -> NavState   [OPTIONAL — only
      SpringAnchorStrategy implements this, for its post-commit flight
      animation; cursor_nav_prototype.py calls it if the strategy has it]

candidates is the same list[(role, name, extents)] cursor_nav_prototype.py
already builds from render_tree(). extents.x/.y/.width/.height are on-screen
pixel coordinates (already offset/scale-corrected — see deckery_walk.py).
stick_x/stick_y are the RAW evdev ABS_X/ABS_Y values (-32767..32767), not
pre-normalized — strategies divide by 32767.0 themselves so they can each
define their own deadzone/response curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class NavState:
    focus_index: int | None  # always a real candidate once any exist — see SpringAnchorStrategy
    dot_pos: tuple[float, float] | None  # where to actually draw the dot (may differ from focus's center)
    armed: bool = True  # commit-threshold hysteresis, per-strategy meaning
    target_index: int | None = None  # candidate currently being aimed at (armed + mid-deflection only)
    aim_angle: float | None = None  # smoothed stick angle while aiming — draw the ray from this, not from
    # wherever target_index happens to be, so it always tracks the actual stick direction (see
    # ray_rect_intersection below — the previous angle-penalty scoring drew the ray to the found
    # target's center, which visibly lagged/snapped to a stale direction instead of following the stick)
    smoothed_x: float = 0.0  # EMA-filtered raw stick axes — raw evdev readings are noisy enough
    smoothed_y: float = 0.0  # that the angle visibly jittered, especially at the far end of a long ray


def rect_center(extents):
    return extents.x + extents.width / 2.0, extents.y + extents.height / 2.0


def ray_rect_intersection(ox, oy, dx, dy, rx, ry, rw, rh):
    """Standard slab-method ray-vs-AABB test. (ox,oy) is the ray origin,
    (dx,dy) a unit direction vector, (rx,ry,rw,rh) the rectangle. Returns
    the distance t>0 along the ray to the FIRST point where it enters the
    rectangle, or None if the ray (forward only, t>0) never hits it.

    This replaces angle-deviation-to-center scoring: a real geometric
    raycast hits a target anywhere across its width/height, not just when
    aimed almost exactly at its center point — "auch wenn ich einfach bis
    zur Kante... treffen würde, müsste es angezeigt werden"."""
    tmin, tmax = 0.0, math.inf
    for o, d, lo, hi in ((ox, dx, rx, rx + rw), (oy, dy, ry, ry + rh)):
        if abs(d) < 1e-9:
            if o < lo or o > hi:
                return None
            continue
        t1, t2 = (lo - o) / d, (hi - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return None
    return tmin if tmin > 1e-6 else None


def _nearest_to(pos, candidates):
    if not candidates:
        return None, None
    px, py = pos
    best_i, best_d = None, None
    for i, (_, _, extents) in enumerate(candidates):
        cx, cy = rect_center(extents)
        d = math.hypot(cx - px, cy - py)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


class SpringAnchorStrategy:
    """See module docstring. Refined from the first cut after live feedback:

    - The elastic stretch cap is no longer a fixed pixel constant. On every
      update (even tiny deflections) this now scores candidates in the
      stick's exact direction the same way a commit would — so the actual
      target and its distance from the anchor are known continuously, not
      just discovered at the moment of commit. The stretch is capped at
      STRETCH_FRACTION (<0.5) of that real distance, so at the commit
      threshold the dot sits just short of the midpoint between anchor and
      target — the point actually observed to look right for both
      closely-spaced and far-apart candidates, rather than a constant that
      overshoots close targets and undershoots far ones.
    - Once committed, the stick is fully inert (ratchet lock, `armed`
      False): update() ignores stick_x/stick_y for positioning entirely
      until magnitude drops back under RELEASE_THRESHOLD — which sits just
      under COMMIT_THRESHOLD, not halfway back to center, and the instant
      it unlocks it re-evaluates the CURRENT stick position immediately
      (no forced re-center first) — so a quick series of small pumping
      motions (push past threshold, ease back slightly, push again) chains
      A->B->C without ever returning to neutral. The dot's actual flight
      to the new anchor happens in tick() — a periodic call independent of
      stick events — so it keeps animating even if the stick is held
      perfectly still right after a commit.

    MAX_STRETCH_PX/COMMIT_THRESHOLD/etc. are the tuning knobs — needs
    hands-on adjustment once this is felt live."""

    STRETCH_FRACTION = 0.45  # fraction of anchor->target distance the dot may stretch toward
    # Absolute ceiling regardless of target_dist — without this, a target
    # hundreds of pixels away made even a tiny stick nudge visibly jump the
    # dot a long way (stretch scales ~linearly with target_dist near t=0
    # on the ease-out curve below), which reads as wildly disproportionate
    # to how little the stick actually moved.
    MAX_STRETCH_ABSOLUTE_PX = 140.0
    FALLBACK_MAX_STRETCH_PX = 55.0  # used only when no candidate scores in that direction at all
    COMMIT_THRESHOLD = 0.5  # stick magnitude (0..1) at which a direction commits to the NEAREST hit
    # Past this (near full deflection), commit to the FARTHEST hit in this
    # direction instead — a deliberate hard push jumps clear across a busy
    # container to its far edge ("aus der Mitte im Browser ... ganz nach
    # links rausspringen zu einem Element, das wirklich links ist") rather
    # than stepping through every element in between one at a time.
    FAR_THRESHOLD = 0.85
    # Deliberately close to COMMIT_THRESHOLD (not halfway to center) — only
    # a small ease-off is needed to re-arm, enabling rapid chained jumps.
    RELEASE_THRESHOLD = COMMIT_THRESHOLD - 0.08
    # Was 0.03 — far too permissive: even a slight resting-hand tremor
    # ("ein leichter Rauch des Daumens") was already enough to send the
    # ball moving. 0.15 needs a deliberate push before anything happens.
    MIN_RESPONSE = 0.15
    SETTLE_RATE_PER_S = 14.0  # how fast tick() eases dot_pos toward the anchor while locked

    def initial_state(self, candidates, screen_center):
        if not candidates:
            return NavState(focus_index=None, dot_pos=None)
        i, _ = _nearest_to(screen_center, candidates)
        return NavState(focus_index=i, dot_pos=rect_center(candidates[i][2]))

    # A pure ray-vs-rectangle hit isn't enough on its own: a wide rectangle
    # directly "above" can still be clipped at its corner by a ray aimed
    # mostly diagonally, even though that diagonal push clearly means a
    # different, genuinely diagonal element — "der Strahl hittet aber
    # trotzdem noch einen kleinen Zipfel von dem Element oben drüber".
    # Candidates whose CENTER lies more than this far off the stick's
    # angle are excluded even if the raycast geometrically intersects them.
    MAX_AIM_ANGLE_DEVIATION = math.radians(35)

    @staticmethod
    def _best_target(anchor_cx, anchor_cy, exclude_index, candidates, angle, farthest=False):
        """Casts a ray from (anchor_cx, anchor_cy) in the exact given
        angle and returns (index, distance) of whichever candidate it hits
        FIRST (smallest positive t, the default) or LAST (farthest=True,
        for FAR_THRESHOLD's long jump), among those whose center is within
        MAX_AIM_ANGLE_DEVIATION of that angle — not the smallest
        angle-deviation alone, and not a geometric hit alone either."""
        dx, dy = math.cos(angle), math.sin(angle)
        best_i, best_t = None, None
        for i, (_, _, extents) in enumerate(candidates):
            if i == exclude_index:
                continue
            t = ray_rect_intersection(anchor_cx, anchor_cy, dx, dy, extents.x, extents.y, extents.width, extents.height)
            if t is None:
                continue
            cx, cy = rect_center(extents)
            center_angle = math.atan2(cy - anchor_cy, cx - anchor_cx)
            deviation = abs((center_angle - angle + math.pi) % (2 * math.pi) - math.pi)
            if deviation > SpringAnchorStrategy.MAX_AIM_ANGLE_DEVIATION:
                continue
            if best_t is None or (t > best_t if farthest else t < best_t):
                best_t, best_i = t, i
        return best_i, best_t

    EMA_ALPHA = 0.35  # 0..1, lower = more smoothing/lag on the raw stick vector

    def update(self, state: NavState, candidates, stick_x, stick_y) -> NavState:
        if state.focus_index is None or state.focus_index >= len(candidates):
            return state
        anchor_cx, anchor_cy = rect_center(candidates[state.focus_index][2])

        # Low-pass filter the raw stick vector before it touches anything
        # else. Raw evdev readings are noisy enough that the angle visibly
        # jitters — and because the ray is drawn all the way to the screen
        # edge, even small angular noise near the anchor becomes a large
        # visible wobble at the far end. Smoothing x/y directly (not the
        # angle — angles wrap at ±pi, which would corrupt an EMA there).
        sx = state.smoothed_x + self.EMA_ALPHA * (stick_x - state.smoothed_x)
        sy = state.smoothed_y + self.EMA_ALPHA * (stick_y - state.smoothed_y)
        magnitude = math.hypot(sx, sy) / 32767.0

        if not state.armed:
            # Ratchet-locked: the stick does nothing positional at all
            # until magnitude drops back under RELEASE_THRESHOLD — tick()
            # drives the actual flight to the new anchor in the meantime.
            if magnitude >= self.RELEASE_THRESHOLD:
                return NavState(
                    focus_index=state.focus_index, dot_pos=state.dot_pos, armed=False, smoothed_x=sx, smoothed_y=sy
                )
            # Just unlocked — fall through immediately using the CURRENT
            # stick position instead of forcing a center-reset frame first,
            # so a quick pump (past threshold, slightly off, past again)
            # chains straight into the next aim/commit.

        if magnitude < self.MIN_RESPONSE:
            return NavState(
                focus_index=state.focus_index, dot_pos=(anchor_cx, anchor_cy), armed=True, smoothed_x=sx, smoothed_y=sy
            )

        angle = math.atan2(sy, sx)
        # Know the real target (and its distance) from the very first
        # sliver of deflection, not just at commit time — "ich weiß ja
        # schon bei der Richtung, was das Ziel sein wird".
        new_target_i, target_dist = self._best_target(anchor_cx, anchor_cy, state.focus_index, candidates, angle)
        if new_target_i is not None:
            target_i = new_target_i
        else:
            # Sticky fallback: a single-frame raycast miss (residual noise
            # even after smoothing, right at a target's angular edge)
            # shouldn't snap the dot back to the fallback stretch — keep
            # whatever target was most recently locked in while still aiming.
            # Re-derive its distance via the SAME ray-edge metric _best_target
            # uses (not center-to-center) — mixing the two here previously
            # made max_stretch jump between two different distance bases
            # frame to frame, which is exactly what caused the dot to
            # visibly overshoot on dense/closely-packed lists.
            target_i = state.target_index
            if target_i is not None and target_i >= len(candidates):
                target_i = None
            if target_i is not None:
                _, _, te = candidates[target_i]
                target_dist = ray_rect_intersection(
                    anchor_cx, anchor_cy, math.cos(angle), math.sin(angle), te.x, te.y, te.width, te.height
                )
                if target_dist is None:
                    # The sticky target is no longer anywhere near this ray
                    # at all (stick moved decisively away) — give up on it
                    # rather than keep reporting a bogus distance.
                    target_i = None

        # The DOT moves along the FIXED anchor->target line — recomputed
        # only when the target itself actually changes, not on every raw
        # angle wobble — "der Ball sollte sich nicht auf der Linie bewegen,
        # nur die Linie soll sich bewegen". The ray (aim_angle, drawn
        # separately) still reflects the live smoothed direction.
        if target_i is not None:
            tx, ty = rect_center(candidates[target_i][2])
            dot_angle = math.atan2(ty - anchor_cy, tx - anchor_cx)
            max_stretch = min(target_dist * self.STRETCH_FRACTION, self.MAX_STRETCH_ABSOLUTE_PX)
        else:
            dot_angle = angle
            max_stretch = self.FALLBACK_MAX_STRETCH_PX

        # Ease-out: responsive immediately even for small deflections, but
        # resistance grows the further it's pulled (diminishing marginal
        # displacement per unit of additional stick push) — "je weiter ich
        # ihn wegziehen will, desto schwerer wird es".
        t = min(magnitude / self.COMMIT_THRESHOLD, 1.0)
        stretch = max_stretch * (1 - (1 - t) ** 2)
        dot_pos = (anchor_cx + stretch * math.cos(dot_angle), anchor_cy + stretch * math.sin(dot_angle))

        if magnitude < self.COMMIT_THRESHOLD or target_i is None:
            # Either not pushed far enough yet, or nothing has ever been
            # hit in this direction — stay armed either way. aim_angle is
            # set regardless of a hit: the ray itself always tracks the
            # live (smoothed) stick direction — "diese Linie müsste auch
            # angezeigt werden, egal in welche Richtung sie schießt".
            return NavState(
                focus_index=state.focus_index, dot_pos=dot_pos, armed=True,
                target_index=target_i, aim_angle=angle, smoothed_x=sx, smoothed_y=sy,
            )

        # Commit. A hard push (>= FAR_THRESHOLD) re-targets to the FARTHEST
        # hit in this exact direction instead of the near one already found
        # above — jumping clear across a busy container to its far edge in
        # one gesture. A near-threshold push commits to the near target as
        # already computed. Either way: lock the stick out (armed=False)
        # and hand off to tick() for the actual flight.
        commit_i = target_i
        if magnitude >= self.FAR_THRESHOLD:
            far_i, _ = self._best_target(anchor_cx, anchor_cy, state.focus_index, candidates, angle, farthest=True)
            if far_i is not None:
                commit_i = far_i
        return NavState(
            focus_index=commit_i, dot_pos=dot_pos, armed=False,
            target_index=None, aim_angle=None, smoothed_x=sx, smoothed_y=sy,
        )

    def tick(self, state: NavState, candidates, dt: float) -> NavState:
        """Advances the post-commit flight animation. No-op while armed
        (update() owns dot_pos then) or once settled. Called on a fixed
        timer independent of stick events — see cursor_nav_prototype.py."""
        if state.armed or state.focus_index is None or state.focus_index >= len(candidates):
            return state
        if state.dot_pos is None:
            return state
        anchor_cx, anchor_cy = rect_center(candidates[state.focus_index][2])
        dx, dy = anchor_cx - state.dot_pos[0], anchor_cy - state.dot_pos[1]
        if math.hypot(dx, dy) < 0.5:
            return state  # settled, nothing to do
        ease = min(1.0, self.SETTLE_RATE_PER_S * dt)
        new_pos = (state.dot_pos[0] + dx * ease, state.dot_pos[1] + dy * ease)
        return NavState(
            focus_index=state.focus_index, dot_pos=new_pos, armed=False,
            smoothed_x=state.smoothed_x, smoothed_y=state.smoothed_y,
        )

    def update_after_render(self, state: NavState, candidates) -> NavState:
        if not candidates:
            return NavState(focus_index=None, dot_pos=None)
        if state.focus_index is None or state.focus_index >= len(candidates):
            # Anchor's element vanished — pick whichever candidate is
            # nearest wherever the dot last was (falls back to its own
            # center if we never had a dot position at all).
            pos = state.dot_pos or rect_center(candidates[0][2])
            i, _ = _nearest_to(pos, candidates)
            return NavState(focus_index=i, dot_pos=rect_center(candidates[i][2]))
        return state


class SemanticGainFieldStrategy:
    """Continuous "fly the dot with the stick" model — no discrete hops, no
    ratchet lock, no raycast. Adapted from Semantic Pointing (Blanch,
    Guiard & Beaudouin-Lafon, CHI2004 — http://iihm.imag.fr/blanch/publications/chi2004/chi2004-sp.html):
    the mouse-pointing technique of warping the control-display ratio near
    targets so they're effectively "bigger" in motor space while staying
    visually unchanged. NOT a Voronoi/bubble-cursor model — those use hard
    per-target regions (nearest-wins, sharp boundary); this uses one
    continuous scalar FIELD summed from every candidate, no boundaries at
    all. The two are easy to conflate; they aren't the same technique.

    Movement is velocity-based: the stick vector directly drives dot
    velocity, scaled by a per-position GAIN that dips near candidates
    (more precision needed) and returns to 1.0 (full speed) in open space
    — "durch den leeren Raum fliegen soll sehr schnell gehen". The same
    stick gesture always means the same thing; only the speed it produces
    varies with what's nearby, so aiming and traversal use one continuous
    motion instead of two different interaction styles.

    gain(P) uses the paper's actual mixing formula, not a simplified one:
        gain(P) = (1 - ΣΩᵢ) · 1  +  Σᵢ Sᵢ · Ωᵢ
        Ωᵢ = sech²(|P - Cᵢ| / Wᵢ), truncated to 0 beyond |P-Cᵢ| = Wᵢ
    Cᵢ/Wᵢ = candidate i's center / influence radius. Sᵢ = candidate i's
    own local gain floor — lower (slower) when more OTHER candidates sit
    close to it, since dense clusters need more precision to tell apart
    ("in einer kleinen Liste von Elementen ist der Raum stärker gekrümmt
    als an einer Stelle mit nur einem Element"). When multiple candidates'
    fields overlap enough that ΣΩᵢ > 1 (acknowledged as possible near
    dense clusters), Ωᵢ is renormalized to sum to 1 rather than letting
    the (1-ΣΩᵢ) baseline term go negative — the paper doesn't need this
    because mouse targets are usually sparser than a packed list here.

    focus_index = whichever candidate is nearest the dot, but ONLY if
    within FOCUS_RADIUS_PX — unlike SpringAnchorStrategy, landing with
    nothing focused is allowed here ("das können wir mal ausprobieren").
    SNAP_PULL_STRENGTH is 0 (off) by default — a later experiment could
    add real magnetic pull-in once this baseline is felt out live.

    Reuses NavState's existing fields (dot_pos/focus_index/smoothed_x/y);
    armed/target_index/aim_angle stay at their defaults, unused here.

    Known cost: _target_scale() is O(candidates) per candidate whose field
    reaches a given point, called fresh every tick — for ~150-200
    candidates (e.g. a big System Settings page) that's a real per-tick
    cost worth profiling if it's ever felt as lag; it only depends on the
    candidate list, not on dot_pos, so caching it once per render (instead
    of recomputing every tick) is the obvious fix if needed.
    """

    BASE_SPEED_PX_PER_SEC = 1500.0
    S_MIN = 0.15  # lowest gain ever, right on top of a target
    S_BASE = 0.35  # gain floor before density adjustment
    DENSITY_WEIGHT = 0.3  # how much each nearby OTHER candidate lowers a target's own Sᵢ further
    DENSITY_RADIUS_PX = 150.0  # neighbor-counting radius for the density adjustment above
    INFLUENCE_MULTIPLIER = 1.6  # how far a candidate's field reaches beyond its own bounding box
    MIN_INFLUENCE_PX = 20.0  # floor on influence radius for very small candidates
    FOCUS_RADIUS_PX = 60.0  # how close the dot must be to count as "on" a candidate
    SNAP_PULL_STRENGTH = 0.0  # 0 = off; magnetic pull-in toward focus, not yet tuned/enabled
    DEADZONE = 0.08
    EMA_ALPHA = 0.35

    @classmethod
    def influence_radius(cls, extents):
        half_diag = math.hypot(extents.width, extents.height) / 2.0
        return max(half_diag, cls.MIN_INFLUENCE_PX) * cls.INFLUENCE_MULTIPLIER

    @classmethod
    def _target_scale(cls, index, candidates):
        cx, cy = rect_center(candidates[index][2])
        n = 0
        for j, (_, _, e) in enumerate(candidates):
            if j == index:
                continue
            jx, jy = rect_center(e)
            if math.hypot(jx - cx, jy - cy) <= cls.DENSITY_RADIUS_PX:
                n += 1
        return max(cls.S_BASE / (1 + cls.DENSITY_WEIGHT * n), cls.S_MIN)

    @classmethod
    def _gain(cls, pos, candidates):
        px, py = pos
        omegas = []
        for i, (_, _, extents) in enumerate(candidates):
            cx, cy = rect_center(extents)
            w = cls.influence_radius(extents)
            u = math.hypot(cx - px, cy - py) / w
            if u >= 1.0:
                continue
            sech_u = 1.0 / math.cosh(u)
            omegas.append((i, sech_u * sech_u))
        if not omegas:
            return 1.0
        total_omega = sum(o for _, o in omegas)
        if total_omega > 1.0:
            return sum(cls._target_scale(i, candidates) * (o / total_omega) for i, o in omegas)
        blend = sum(cls._target_scale(i, candidates) * o for i, o in omegas)
        return (1.0 - total_omega) + blend

    def initial_state(self, candidates, screen_center):
        if not candidates:
            return NavState(focus_index=None, dot_pos=None)
        i, d = _nearest_to(screen_center, candidates)
        pos = rect_center(candidates[i][2])
        focus = i if d is not None and d <= self.FOCUS_RADIUS_PX else None
        return NavState(focus_index=focus, dot_pos=pos)

    def update(self, state: NavState, candidates, stick_x, stick_y) -> NavState:
        # Only smooths/stores the raw stick reading — the actual flight
        # integration happens in tick() (needs to run continuously, not
        # just once per stick event, for velocity-based movement to feel
        # like flying rather than a series of tiny steps).
        sx = state.smoothed_x + self.EMA_ALPHA * (stick_x - state.smoothed_x)
        sy = state.smoothed_y + self.EMA_ALPHA * (stick_y - state.smoothed_y)
        return NavState(focus_index=state.focus_index, dot_pos=state.dot_pos, smoothed_x=sx, smoothed_y=sy)

    def tick(self, state: NavState, candidates, dt: float) -> NavState:
        if state.dot_pos is None or not candidates:
            return state
        vx, vy = state.smoothed_x / 32767.0, state.smoothed_y / 32767.0
        if math.hypot(vx, vy) < self.DEADZONE:
            return state
        gain = self._gain(state.dot_pos, candidates)
        speed = self.BASE_SPEED_PX_PER_SEC * gain
        new_pos = (state.dot_pos[0] + vx * speed * dt, state.dot_pos[1] + vy * speed * dt)
        i, d = _nearest_to(new_pos, candidates)
        focus = i if d is not None and d <= self.FOCUS_RADIUS_PX else None
        return NavState(focus_index=focus, dot_pos=new_pos, smoothed_x=state.smoothed_x, smoothed_y=state.smoothed_y)

    def update_after_render(self, state: NavState, candidates) -> NavState:
        if not candidates:
            return NavState(focus_index=None, dot_pos=None, smoothed_x=state.smoothed_x, smoothed_y=state.smoothed_y)
        if state.dot_pos is None:
            i, _ = _nearest_to((0, 0), candidates)
            return NavState(focus_index=i, dot_pos=rect_center(candidates[i][2]))
        i, d = _nearest_to(state.dot_pos, candidates)
        focus = i if d is not None and d <= self.FOCUS_RADIUS_PX else None
        return NavState(
            focus_index=focus, dot_pos=state.dot_pos, smoothed_x=state.smoothed_x, smoothed_y=state.smoothed_y
        )


class FreeMagnetStrategy:
    """Dot moves by a fixed screen-space step per discrete stick push, in
    the stick's exact analog direction, and magnetically snaps onto a real
    element only when within SNAP_RADIUS_PX — otherwise floats freely with
    nothing focused. Kept for reference/comparison; SpringAnchorStrategy
    replaced it as the active default because a floating "nothing focused"
    state turned out not to be wanted — only real elements should ever be
    targetable. See this module's docstring for the fuller comparison.

    Uses the discrete-step interface (step(), not update()) from its
    original design — NOT currently wired into cursor_nav_prototype.py's
    continuous update() call. If reviving this, cursor_nav_prototype.py's
    stick handling needs its old discrete deadzone/armed edge-detection
    back (removed when SpringAnchorStrategy's continuous model replaced it).
    """

    STEP_SIZE_PX = 90.0
    SNAP_RADIUS_PX = 70.0

    def initial_state(self, candidates, screen_center):
        if not candidates:
            return NavState(dot_pos=None, focus_index=None)
        i, _ = _nearest_to(screen_center, candidates)
        return self._snap(rect_center(candidates[i][2]), candidates)

    def step(self, state: NavState, candidates, stick_angle: float) -> NavState:
        if state.dot_pos is None:
            return state
        px, py = state.dot_pos
        px += self.STEP_SIZE_PX * math.cos(stick_angle)
        py += self.STEP_SIZE_PX * math.sin(stick_angle)
        return self._snap((px, py), candidates)

    def update_after_render(self, state: NavState, candidates) -> NavState:
        if state.dot_pos is None:
            return state
        return self._snap(state.dot_pos, candidates, allow_move=False)

    def _snap(self, pos, candidates, allow_move=True):
        i, d = _nearest_to(pos, candidates)
        if i is not None and d <= self.SNAP_RADIUS_PX:
            snapped_pos = rect_center(candidates[i][2]) if allow_move else pos
            return NavState(dot_pos=snapped_pos, focus_index=i)
        return NavState(dot_pos=pos, focus_index=None)
