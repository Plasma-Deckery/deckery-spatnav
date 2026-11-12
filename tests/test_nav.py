import math
import unittest

from deckery_atspi.geometry import Rect
from deckery_nav.absolute import AbsoluteParams, AbsolutePicker, choose_area, pad_to_area
from deckery_nav.direction import DirectionParams, pick, quantize
from deckery_nav.makima_input import ExportKeeper, InputSnapshot, parse_state
from deckery_nav.presets import DEFAULT_PATH, load_presets, parse_presets
from deckery_nav.stick import StickGate, StickParams
from deckery_nav.targets import Target, nearest, targets_from_json

RIGHT, DOWN, LEFT, UP = 0.0, math.pi / 2, math.pi, -math.pi / 2


def target(tid, x, y, w, h, window="w1", visible=None):
    r = Rect(x, y, w, h)
    vis = tuple(visible) if visible else (r,)
    return Target(tid, window, "push button", tid, r, vis, r.center)


def grid():
    """3×3 buttons, 100 px pitch, 60×30 each:
         a b c
         d e f
         g h i"""
    out = []
    for row, names in enumerate(("abc", "def", "ghi")):
        for col, n in enumerate(names):
            out.append(target(n, 100 + col * 100, 100 + row * 100, 60, 30))
    return out


def by_id(targets):
    return {t.id: t for t in targets}


class DirectionTest(unittest.TestCase):
    def setUp(self):
        self.g = grid()
        self.t = by_id(self.g)

    def check(self, scorer, origin, angle, expect, **kw):
        got = pick(self.t[origin], self.g, angle, DirectionParams(scorer=scorer, **kw))
        self.assertEqual(got.id if got else None, expect, f"{scorer} {origin} {angle:.2f}")

    def test_all_scorers_move_along_rows_and_columns(self):
        for scorer in ("kante", "kegel", "strahl"):
            self.check(scorer, "e", RIGHT, "f")
            self.check(scorer, "e", LEFT, "d")
            self.check(scorer, "e", UP, "b")
            self.check(scorer, "e", DOWN, "h")

    def test_nothing_beyond_the_edge(self):
        for scorer in ("kante", "kegel", "strahl"):
            self.check(scorer, "c", RIGHT, None)
            self.check(scorer, "a", UP, None)

    def test_diagonal(self):
        self.check("kegel", "e", math.atan2(1, 1), "i")
        self.check("kante", "e", math.atan2(1, 1), "i")

    def test_kante_prefers_the_row_over_a_closer_diagonal(self):
        # x sits below-right, closer to e than f is, but off the row.
        g = self.g + [target("x", 265, 250, 20, 20)]
        got = pick(by_id(g)["e"], g, RIGHT, DirectionParams(scorer="kante"))
        self.assertEqual(got.id, "f")

    def test_kegel_respects_the_cone(self):
        # Only c is to the upper right; with a narrow cone straight right
        # from a there is b, from c nothing.
        self.check("kegel", "a", RIGHT, "b", cone_deg=10)
        self.check("kegel", "g", math.atan2(-1, 3), None, cone_deg=5)

    def test_strahl_needs_a_hit_or_falls_back_to_the_cone(self):
        # A beam straight up from g hits d, not a.
        self.check("strahl", "g", UP, "d")
        # A ray between columns hits nothing → cone fallback still finds something.
        got = pick(self.t["e"], self.g, math.atan2(-1, 0.6), DirectionParams(scorer="strahl", beam_px=2))
        self.assertIsNotNone(got)

    def test_kante_ignores_what_starts_beside_the_origin(self):
        # A wide slider above-right starting at e's left edge is not "right of e".
        g = [target("e", 200, 200, 60, 30), target("slider", 200, 150, 300, 20)]
        self.assertIsNone(pick(by_id(g)["e"], g, RIGHT, DirectionParams(scorer="kante")))

    def test_skips_targets_sharing_the_origin_spot(self):
        g = self.g + [target("e-outer", 100, 200, 260, 30)]   # a row container around e
        got = pick(by_id(g)["e"], g, RIGHT, DirectionParams(scorer="kante"))
        self.assertEqual(got.id, "f")

    def test_quantize(self):
        self.assertAlmostEqual(quantize(0.3, 4), 0.0)
        self.assertAlmostEqual(quantize(0.6, 8), math.pi / 4)
        self.assertAlmostEqual(quantize(0.6, 0), 0.6)


class StickTest(unittest.TestCase):
    def test_push_fires_once_then_repeats(self):
        gate = StickGate(StickParams(repeat_delay=0.4, repeat_interval=0.1))
        self.assertIsNone(gate.step(0.3, 0, 0.0))          # below on
        self.assertAlmostEqual(gate.step(0.9, 0, 0.01), 0.0)
        self.assertIsNone(gate.step(0.9, 0, 0.2))           # held, before delay
        self.assertIsNotNone(gate.step(0.9, 0, 0.42))       # first repeat
        self.assertIsNone(gate.step(0.9, 0, 0.45))
        self.assertIsNotNone(gate.step(0.9, 0, 0.53))

    def test_hysteresis_rearms_only_below_off(self):
        gate = StickGate(StickParams(on=0.6, off=0.3, repeat_interval=0))
        self.assertIsNotNone(gate.step(0.7, 0, 0))
        self.assertIsNone(gate.step(0.4, 0, 1))      # between off and on: still held
        self.assertIsNone(gate.step(0.7, 0, 2))
        self.assertIsNone(gate.step(0.2, 0, 3))      # released
        self.assertIsNotNone(gate.step(0.7, 0, 4))

    def test_up_is_screen_up(self):
        gate = StickGate()
        self.assertAlmostEqual(gate.step(0, 0.9, 0), UP)

    def test_turning_the_held_stick_retriggers(self):
        gate = StickGate(StickParams(repeat_delay=10, retrigger_deg=45))
        gate.step(0.9, 0, 0)
        self.assertIsNone(gate.step(0.9, 0.2, 0.1))         # ~13°: same push
        self.assertAlmostEqual(gate.step(0, -0.9, 0.2), DOWN)


class AbsoluteTest(unittest.TestCase):
    def setUp(self):
        self.g = grid()

    def test_pad_maps_onto_the_area_with_y_up(self):
        area = Rect(0, 0, 1000, 500)
        p = AbsoluteParams(edge=0.0)
        self.assertEqual(pad_to_area(-1, 1, area, p), (0, 0))         # top left
        self.assertEqual(pad_to_area(1, -1, area, p), (1000, 500))
        self.assertEqual(pad_to_area(0, 0, area, p), (500, 250))
        self.assertEqual(pad_to_area(-1, 1, area, AbsoluteParams(edge=0.0, invert_y=True)), (0, 500))

    def test_edge_border_is_clamped(self):
        area = Rect(0, 0, 100, 100)
        p = AbsoluteParams(edge=0.1)
        self.assertEqual(pad_to_area(-0.9, 0.9, area, p), (0, 0))
        self.assertEqual(pad_to_area(-1, 1, area, p), (0, 0))
        self.assertAlmostEqual(pad_to_area(0, 0, area, p)[0], 50)

    def test_picker_selects_under_and_near_the_finger(self):
        picker = AbsolutePicker(AbsoluteParams(snap_px=20, keep_px=0))
        self.assertEqual(picker.update(self.g, 110, 110, 0).id, "a")
        self.assertEqual(picker.update(self.g, 170, 115, 0).id, "a")   # 10 px right of a
        self.assertIsNone(picker.update(self.g, 2000, 2000, 0))        # far from everything

    def test_hysteresis_holds_near_the_border(self):
        picker = AbsolutePicker(AbsoluteParams(keep_px=30))
        picker.update(self.g, 110, 110, 0)
        # Gap between a (ends x=160) and b (starts x=200): at x=185 b is nearer
        # but a still holds.
        self.assertEqual(picker.update(self.g, 185, 115, 0).id, "a")
        self.assertEqual(picker.update(self.g, 205, 115, 0).id, "b")

    def test_nested_target_wins_inside_hysteresis(self):
        row = target("row", 100, 100, 260, 30)
        btn = target("btn", 300, 105, 20, 20)
        picker = AbsolutePicker(AbsoluteParams(keep_px=30))
        self.assertEqual(picker.update([row, btn], 150, 110, 0).id, "row")
        self.assertEqual(picker.update([row, btn], 310, 110, 0).id, "btn")

    def test_dwell(self):
        picker = AbsolutePicker(AbsoluteParams(keep_px=0, dwell_ms=100))
        self.assertEqual(picker.update(self.g, 110, 110, 0.0).id, "a")   # touch-down: at once
        self.assertEqual(picker.update(self.g, 210, 110, 0.2).id, "a")   # b pending
        self.assertEqual(picker.update(self.g, 210, 110, 0.31).id, "b")

    def test_smoothing(self):
        picker = AbsolutePicker(AbsoluteParams(smoothing=0.5))
        self.assertEqual(picker.finger(0, 0), (0, 0))
        self.assertEqual(picker.finger(100, 0), (50, 0))
        picker.lift()
        self.assertEqual(picker.finger(100, 0), (100, 0))

    def test_areas(self):
        monitors = [Rect(0, 0, 1000, 600), Rect(1000, 0, 800, 600)]
        g = self.g + [target("other", 1200, 300, 50, 50, window="w2")]
        self.assertEqual(choose_area("bildschirm", monitors, g, None), Rect(0, 0, 1800, 600))
        self.assertEqual(choose_area("monitor", monitors, g, by_id(g)["other"]), monitors[1])
        self.assertEqual(choose_area("fenster", monitors, g, by_id(g)["a"]), Rect(100, 100, 260, 230))
        self.assertEqual(choose_area("fenster", monitors, g, None), Rect(0, 0, 1800, 600))
        self.assertEqual(choose_area("ziele", monitors, g, None), Rect(100, 100, 1150, 250))


class TargetsTest(unittest.TestCase):
    def test_from_json_drops_invisible_and_tiny(self):
        els = [
            {"id": "a", "window": "w", "role": "push button", "name": "A", "rect": [0, 0, 50, 20],
             "visible": [[0, 0, 50, 20]], "target": [25, 10]},
            {"id": "sliver", "window": "w", "role": "push button", "name": "", "rect": [0, 0, 50, 20],
             "visible": [[0, 0, 3, 20]], "target": [1, 10]},
            {"id": "gone", "window": "w", "role": "push button", "name": "", "rect": [0, 0, 50, 20],
             "visible": [], "target": [0, 0]},
        ]
        self.assertEqual([t.id for t in targets_from_json(els, min_size=6)], ["a"])
        self.assertEqual(len(targets_from_json(els)), 2)

    def test_nearest_prefers_the_smallest_containing(self):
        row = target("row", 0, 0, 300, 30)
        btn = target("btn", 10, 5, 20, 20)
        self.assertEqual(nearest([row, btn], 15, 10).id, "btn")
        self.assertEqual(nearest([row, btn], 200, 10).id, "row")


class MakimaInputTest(unittest.TestCase):
    def test_parse(self):
        s = parse_state({"context": {"analog_state_export": True, "paused": False},
                         "trackpads": {"left": {"x": 0.5, "y": -0.25, "touching": True, "pressed": False}},
                         "sticks": {"lstick": {"x": 0.1, "y": 0.9}}})
        self.assertTrue(s.analog and s.lpad.touching)
        self.assertEqual((s.lpad.x, s.lpad.y), (0.5, -0.25))
        self.assertEqual(s.lstick, (0.1, 0.9))

    def test_parse_without_analog(self):
        s = parse_state({"context": {"analog_state_export": False, "paused": True},
                         "trackpads": None, "sticks": None})
        self.assertFalse(s.analog)
        self.assertTrue(s.paused)

    def test_export_keeper_rate_limits_and_exit(self):
        k = ExportKeeper(min_interval=1.0)
        off, on = InputSnapshot(analog=False), InputSnapshot(analog=True)
        self.assertFalse(k.want_off_at_exit(off))          # never sent: leave it alone
        self.assertTrue(k.want_on(off, 0.0))
        self.assertFalse(k.want_on(off, 0.5))
        self.assertFalse(k.want_on(on, 2.0))
        self.assertTrue(k.want_on(off, 2.0))
        self.assertTrue(k.want_off_at_exit(on))
        self.assertFalse(k.want_off_at_exit(InputSnapshot(analog=True, paused=True)))   # HUD open


class PresetsTest(unittest.TestCase):
    def test_shipped_file_loads(self):
        p = load_presets(DEFAULT_PATH)
        self.assertGreaterEqual(len(p.relative), 2)
        self.assertGreaterEqual(len(p.absolute), 2)
        for r in p.relative:
            self.assertIn(r.direction.scorer, ("kante", "kegel", "strahl"))
            self.assertIn(r.stick.directions, (0, 4, 8))
        for a in p.absolute:
            self.assertIn(a.params.area, ("bildschirm", "monitor", "ziele", "fenster"))
            self.assertIn(a.params.pointer, ("ziel", "finger", "aus"))

    def test_overrides_and_defaults(self):
        p = parse_presets({"relative": {"x": {"scorer": "kegel", "repeat_interval": 0}},
                           "absolute": {"y": {"keep_px": 5}}})
        self.assertEqual(p.find("relative", "x").stick.repeat_interval, 0)
        self.assertEqual(p.find("relative", "x").stick.on, StickParams().on)
        self.assertEqual(p.find("absolute", "y").params.keep_px, 5)
        self.assertIsNone(p.find("absolute", "nope"))


class NavTest(unittest.TestCase):
    """Both inputs through the state object, with a fake makima snapshot."""

    def setUp(self):
        from deckery_nav.nav import Nav
        els = [{"id": t.id, "window": t.window, "role": t.role, "name": t.name,
                "rect": [t.rect.x, t.rect.y, t.rect.w, t.rect.h],
                "visible": [[v.x, v.y, v.w, v.h] for v in t.visible], "target": list(t.point)}
               for t in grid()]
        self.nav = Nav(load_presets(DEFAULT_PATH), [Rect(0, 0, 460, 430)])
        self.nav.set_elements(els)

    def snap(self, stick=(0.0, 0.0), pad=None):
        from deckery_nav.makima_input import Pad
        return InputSnapshot(lpad=pad or Pad(), lstick=stick, analog=True)

    def test_first_push_selects_near_the_centre_then_jumps(self):
        nav = self.nav
        self.assertEqual(nav.on_input(self.snap((0.9, 0)), 0.0), (230, 215))   # e = centre
        self.assertEqual(nav.selection.id, "e")
        nav.on_input(self.snap(), 0.1)
        self.assertEqual(nav.on_input(self.snap((0.9, 0)), 0.2), (330, 215))
        self.assertEqual(nav.selection.id, "f")

    def test_pad_selects_and_the_stick_continues_from_there(self):
        from deckery_nav.makima_input import Pad
        nav = self.nav
        nav.select_preset("absolute", name="bildschirm")
        # Pad top-left area → a (edge 0.08 maps the border to the screen edge).
        warp = nav.on_input(self.snap(pad=Pad(-0.45, 0.45, True)), 0.0)
        self.assertEqual(nav.selection.id, "a")
        self.assertEqual(warp, (130, 115))
        nav.on_input(self.snap(), 0.1)                    # lift: selection stays
        self.assertEqual(nav.selection.id, "a")
        nav.on_input(self.snap((0, -0.9)), 0.2)           # stick down
        self.assertEqual(nav.selection.id, "d")

    def test_preset_switching(self):
        nav = self.nav
        first = nav.relative.name
        nav.select_preset("relative", step=1)
        self.assertNotEqual(nav.relative.name, first)
        nav.select_preset("relative", step=-1)
        self.assertEqual(nav.relative.name, first)
        self.assertFalse(nav.select_preset("absolute", name="nope"))

    def test_selection_survives_a_new_list_and_falls_back_when_gone(self):
        nav = self.nav
        nav.on_input(self.snap((0.9, 0)), 0.0)            # e
        remaining = [e for e in nav.elements if e["id"] != "e"]
        nav.set_elements(remaining)
        self.assertIsNotNone(nav.selection)
        self.assertNotEqual(nav.selection.id, "e")


if __name__ == "__main__":
    unittest.main()
