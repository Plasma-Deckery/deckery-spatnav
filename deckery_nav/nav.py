"""The navigation state: target list, selection, both inputs (no GTK, no I/O).

on_input() takes one makima snapshot and returns where the pointer should
go (or None); the app does the warping and drawing."""

import math
from dataclasses import dataclass

from .absolute import AbsolutePicker, choose_area, pad_to_area
from .direction import pick, quantize
from .stick import StickGate
from .targets import bounding_box, nearest, targets_from_json


@dataclass
class Aim:
    angle: float
    preview: object      # Target the stick would jump to, or None


class Nav:
    def __init__(self, presets, monitors):
        self.presets = presets
        self.monitors = monitors        # list of Rect
        self.elements = []
        self.targets = []
        self.selection = None
        self.last_point = None          # last pointer position we set / selection point
        self.rel_index = 0
        self.abs_index = 0
        self.touching = False
        self.area = None
        self.finger_point = None
        self.aim = None
        self.jumps = 0
        self._apply_presets()

    # ── presets ──────────────────────────────────────────────────────────
    @property
    def relative(self):
        return self.presets.relative[self.rel_index]

    @property
    def absolute(self):
        return self.presets.absolute[self.abs_index]

    def _apply_presets(self):
        self.gate = StickGate(self.relative.stick)
        cur = getattr(self, "picker", None)
        self.picker = AbsolutePicker(self.absolute.params)
        if cur is not None:
            self.picker.current = cur.current

    def set_presets(self, presets):
        rel, ab = self.relative.name, self.absolute.name
        self.presets = presets
        self.rel_index = next((i for i, p in enumerate(presets.relative) if p.name == rel), 0)
        self.abs_index = next((i for i, p in enumerate(presets.absolute) if p.name == ab), 0)
        self.set_elements(self.elements)
        self._apply_presets()

    def select_preset(self, kind, name=None, step=0):
        """kind 'relative'|'absolute'; by name, or step through the list."""
        items = getattr(self.presets, kind)
        index = self.rel_index if kind == "relative" else self.abs_index
        if name is not None:
            found = [i for i, p in enumerate(items) if p.name == name]
            if not found:
                return False
            index = found[0]
        else:
            index = (index + step) % len(items)
        if kind == "relative":
            self.rel_index = index
        else:
            self.abs_index = index
        self._apply_presets()
        return True

    # ── targets ──────────────────────────────────────────────────────────
    def set_elements(self, elements):
        self.elements = elements
        self.targets = targets_from_json(elements, self.presets.min_size)
        if self.selection is not None:
            same = next((t for t in self.targets if t.id == self.selection.id), None)
            if same is None and self.last_point is not None:
                # Gone (closed, scrolled away): the target now nearest the
                # spot keeps relative navigation going from there.
                same = nearest(self.targets, *self.last_point)
            self.selection = same
            self.picker.current = same

    def _select(self, target):
        self.selection = target
        self.picker.current = target
        if target is not None:
            self.last_point = target.point

    def _screen(self):
        return bounding_box(self.monitors)

    # ── input ────────────────────────────────────────────────────────────
    def on_input(self, snap, now):
        """→ (x, y) for the pointer, or None."""
        warp = None
        warp = self._on_pad(snap.lpad, now) or warp
        warp = self._on_stick(snap.lstick, now) or warp
        return warp

    def _on_pad(self, pad, now):
        params = self.absolute.params
        if not pad.touching:
            if self.touching:
                self.touching = False
                self.picker.lift()
                self.finger_point = None
            return None
        if not self.touching:
            self.touching = True
            self.area = choose_area(params.area, self.monitors, self.targets, self.selection)
        fx, fy = self.picker.finger(*pad_to_area(pad.x, pad.y, self.area, params))
        self.finger_point = (fx, fy)
        before = self.selection
        chosen = self.picker.update(self.targets, fx, fy, now)
        if chosen is not None:
            self._select(chosen)
        else:
            self.selection = None
        if params.pointer == "finger":
            return (fx, fy)
        if params.pointer == "ziel" and chosen is not None and (before is None or before.id != chosen.id):
            self.jumps += 1
            return chosen.point
        return None

    def _on_stick(self, stick, now):
        x, y = stick
        rel = self.relative
        angle = self.gate.step(x, y, now)
        if math.hypot(x, y) >= rel.stick.off and self.selection is not None:
            a = quantize(math.atan2(-y, x), rel.stick.directions)
            self.aim = Aim(a, pick(self.selection, self.targets, a, rel.direction))
        else:
            self.aim = None
        if angle is None:
            return None
        if self.selection is None:
            # Nothing selected yet: start at the target nearest the last
            # pointer spot (or the screen centre) instead of jumping blind.
            screen = self._screen()
            ref = self.last_point or (screen.center if screen else (0, 0))
            target = nearest(self.targets, *ref)
        else:
            target = pick(self.selection, self.targets, quantize(angle, rel.stick.directions),
                          rel.direction)
        if target is None:
            return None
        self._select(target)
        self.jumps += 1
        return target.point if rel.pointer == "ziel" else None

    def state(self):
        s = self.selection
        return {"relative": self.relative.name, "absolute": self.absolute.name,
                "selection": None if s is None else {"id": s.id, "role": s.role, "name": s.name,
                                                     "point": list(s.point)},
                "targets": len(self.targets), "touching": self.touching,
                "finger": list(self.finger_point) if self.finger_point else None,
                "jumps": self.jumps}
