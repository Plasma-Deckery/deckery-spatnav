import unittest

from deckery_atspi.geometry import Rect
from deckery_atspi.placement import placement_for
from deckery_atspi.scene import scene_from_payload


def scene_with(window):
    return scene_from_payload({"currentDesktop": "d", "outputs": [
        {"name": "o", "geometry": {"x": 0, "y": 0, "w": 1164, "h": 727}, "scale": 1.1}],
        "windows": [window]})


# Flatseal (GTK4, CSD) on the test desktop 2026-11-11: frame/client at 0,0,
# buffer 25.45 px larger on every side for the shadow.
GTK4_CSD = {"id": "w", "pid": 1, "layer": 2, "output": "o",
            "frame": {"x": 0, "y": 0, "w": 700, "h": 650},
            "client": {"x": 0, "y": 0, "w": 700, "h": 650},
            "buffer": {"x": -25.45, "y": -25.45, "w": 750.9, "h": 700.9}}


class PlacementTest(unittest.TestCase):
    def assertRect(self, actual, expected):
        for a, e in zip((actual.x, actual.y, actual.w, actual.h), (expected.x, expected.y, expected.w, expected.h)):
            self.assertAlmostEqual(a, e, places=6)

    def test_gtk4_uses_client_origin(self):
        scene = scene_with(GTK4_CSD)
        p = placement_for(scene, scene.windows[0], "gtk", "4.20.1")
        self.assertRect(p.to_screen(Rect(6, 104, 168, 56)), Rect(6, 104, 168, 56))

    def test_gtk3_and_gecko_use_buffer_origin(self):
        scene = scene_with(GTK4_CSD)
        for toolkit, version in (("gtk", "3.24.41"), ("gecko", "140.0")):
            p = placement_for(scene, scene.windows[0], toolkit, version)
            self.assertAlmostEqual(p.to_screen(Rect(30, 30, 10, 10)).x, 4.55)

    def test_chromium_contents_divided_by_output_scale_but_not_titlebar_buttons(self):
        scene = scene_with(dict(GTK4_CSD, buffer={"x": 10, "y": 20, "w": 700, "h": 650}))
        p = placement_for(scene, scene.windows[0], "Chromium")
        self.assertRect(p.to_screen(Rect(110, 220, 11, 11), "Send"), Rect(110, 220, 10, 10))
        self.assertRect(p.to_screen(Rect(110, 220, 11, 11), "Close"), Rect(120, 240, 11, 11))


if __name__ == "__main__":
    unittest.main()
