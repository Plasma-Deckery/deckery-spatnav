import json
import os
import unittest

from deckery_atspi.geometry import Rect, region_area
from deckery_atspi.scene import scene_from_payload
from deckery_atspi.visibility import visible_part, visible_regions

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def win(id, frame, layer=2, desktops=("d1",), **kw):
    return {"id": id, "pid": 1, "caption": id, "layer": layer,
            "frame": dict(zip("xywh", frame)), "desktops": list(desktops), **kw}


def scene(*windows, outputs=({"name": "o1", "geometry": {"x": 0, "y": 0, "w": 1000, "h": 800}},)):
    return scene_from_payload({"currentDesktop": "d1", "outputs": list(outputs),
                               "windows": list(windows)})


class RectTest(unittest.TestCase):
    def test_subtract_middle_leaves_four_pieces_with_correct_area(self):
        pieces = Rect(0, 0, 100, 100).subtract(Rect(25, 25, 50, 50))
        self.assertEqual(len(pieces), 4)
        self.assertAlmostEqual(region_area(pieces), 100 * 100 - 50 * 50)

    def test_subtract_disjoint_is_identity(self):
        self.assertEqual(Rect(0, 0, 10, 10).subtract(Rect(20, 20, 5, 5)), [Rect(0, 0, 10, 10)])

    def test_subtract_full_cover_is_empty(self):
        self.assertEqual(Rect(10, 10, 10, 10).subtract(Rect(0, 0, 100, 100)), [])


class VisibilityTest(unittest.TestCase):
    def test_window_fully_covered_is_not_visible(self):
        regions = visible_regions(scene(win("below", (100, 100, 200, 200)),
                                        win("above", (0, 0, 500, 500))))
        self.assertNotIn("below", regions)
        self.assertIn("above", regions)

    def test_partial_cover_keeps_uncovered_part(self):
        regions = visible_regions(scene(win("below", (0, 0, 400, 400)),
                                        win("above", (200, 0, 400, 400))))
        self.assertAlmostEqual(region_area(regions["below"]), 200 * 400)

    def test_minimized_hidden_and_other_desktop_do_not_occlude(self):
        regions = visible_regions(scene(
            win("base", (0, 0, 400, 400)),
            win("min", (0, 0, 400, 400), minimized=True),
            win("hid", (0, 0, 400, 400), hidden=True),
            win("other", (0, 0, 400, 400), desktops=("d2",)),
        ))
        self.assertEqual(set(regions), {"base"})
        self.assertAlmostEqual(region_area(regions["base"]), 400 * 400)

    def test_overlay_layer_neither_occludes_nor_is_a_target(self):
        regions = visible_regions(scene(win("app", (0, 0, 400, 400)),
                                        win("hud", (0, 0, 1000, 800), layer=9)))
        self.assertEqual(set(regions), {"app"})

    def test_offscreen_part_is_clipped_to_outputs(self):
        regions = visible_regions(scene(win("app", (900, 0, 400, 100))))
        self.assertAlmostEqual(region_area(regions["app"]), 100 * 100)

    def test_window_spanning_two_outputs(self):
        outputs = ({"name": "a", "geometry": {"x": 0, "y": 0, "w": 1000, "h": 800}},
                   {"name": "b", "geometry": {"x": 1000, "y": 0, "w": 1000, "h": 800}})
        regions = visible_regions(scene(win("app", (900, 0, 200, 100)), outputs=outputs))
        self.assertAlmostEqual(region_area(regions["app"]), 200 * 100)

    def test_element_half_covered_by_other_window(self):
        regions = visible_regions(scene(win("below", (0, 0, 400, 400)),
                                        win("above", (200, 0, 400, 400))))
        button = Rect(150, 10, 100, 30)
        part = visible_part(regions["below"], button)
        self.assertAlmostEqual(region_area(part), 50 * 30)


class RealSceneTest(unittest.TestCase):
    """KWin probe dump from 2026-11-10: Claude maximized on its desktop,
    other apps on other desktops, plasmashell panels, our own overlay."""

    def setUp(self):
        self.scene = scene_from_payload(load("kwin_scene_single_output.json"))
        self.regions = visible_regions(self.scene)
        self.by_class = {w.id: w.resource_class for w in self.scene.windows}

    def test_output_scale_comes_from_kwin(self):
        self.assertEqual([(o.name, o.scale) for o in self.scene.outputs], [("eDP-1", 1.1)])

    def test_only_current_desktop_windows_and_panels_are_visible(self):
        visible = sorted((self.by_class[i], self.kind(i)) for i in self.regions)
        # Claude (7..1157 x 31..721) leaves slivers of the desktop background
        # visible; the two auto-hidden panels are hidden; Firefox/Steam/etc.
        # live on other desktops.
        self.assertEqual(visible, [("com.anthropic.Claude", "normal"),
                                   ("plasmashell", "desktop"),
                                   ("plasmashell", "dock"),
                                   ("plasmashell", "dock")])

    def kind(self, window_id):
        flags = next(w.flags for w in self.scene.windows if w.id == window_id)
        if "desktopWindow" in flags:
            return "desktop"
        return "dock" if "dock" in flags else "normal"

    def test_top_panel_cuts_nothing_off_claude(self):
        # Claude's frame starts at y=31, the top panel ends at y=40 → the
        # strip 31..40 is covered.
        claude = next(w for w in self.scene.windows if w.resource_class == "com.anthropic.Claude")
        covered = claude.frame.area - region_area(self.regions[claude.id])
        self.assertGreater(covered, 0)


if __name__ == "__main__":
    unittest.main()
