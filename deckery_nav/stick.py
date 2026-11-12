"""Stick deflection → discrete jump commands (pure state machine).

Push past `on` → one jump at once. Held: after `repeat_delay` s more jumps
every `repeat_interval` s (0 = no repeat). Turning the held stick by more than
`retrigger_deg` counts as a new push. Back below `off` (hysteresis, off < on)
re-arms. Stick y is makima's: up = positive; angles come out in screen space
(0 = right, π/2 = down)."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StickParams:
    on: float = 0.55
    off: float = 0.35
    repeat_delay: float = 0.40
    repeat_interval: float = 0.15
    retrigger_deg: float = 50.0
    directions: int = 8           # 4, 8, or 0 = free angle


@dataclass
class StickGate:
    params: StickParams = StickParams()
    armed: bool = True
    held_angle: float = 0.0
    next_fire: float = 0.0

    def step(self, x, y, now):
        """One stick reading → the angle to jump towards, or None."""
        p = self.params
        mag = math.hypot(x, y)
        if mag < p.off:
            self.armed = True
            return None
        angle = math.atan2(-y, x)
        if self.armed:
            if mag < p.on:
                return None
            self.armed = False
            return self._fire(angle, now, p.repeat_delay)
        if _angle_between(angle, self.held_angle) > math.radians(p.retrigger_deg) and mag >= p.on:
            return self._fire(angle, now, p.repeat_delay)
        if p.repeat_interval > 0 and now >= self.next_fire:
            return self._fire(angle, now, p.repeat_interval)
        return None

    def _fire(self, angle, now, wait):
        self.held_angle = angle
        self.next_fire = now + wait
        return angle


def _angle_between(a, b):
    d = (a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)
