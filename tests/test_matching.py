import json
import os
import unittest
from types import SimpleNamespace

from deckery_atspi.geometry import Rect
from deckery_atspi.matching import match_windows, pick_popover
from deckery_atspi.scene import scene_from_payload

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class RealMatchingTest(unittest.TestCase):
    """Live dump 2026-11-10: Konsole, Trayscale (Flatpak), System Settings,
    Firefox (Flatpak), Steam (13 Chromium frames), QEMU, Claude (Chromium
    CSD), plasmashell desktop + 4 panels, our own overlay."""

    @classmethod
    def setUpClass(cls):
        cls.scene = scene_from_payload(load("kwin_scene_mixed.json"))
        cls.frames = [SimpleNamespace(**f) for f in load("atspi_frames_mixed.json")]
        windows = [w for w in cls.scene.windows if w.layer < 9]
        cls.matches, cls.unmatched = match_windows(windows, cls.frames)
        cls.by_class = {}
        for w in windows:
            cls.by_class.setdefault(w.resource_class, []).append(w)

    def frame_for(self, resource_class, index=0):
        window = self.by_class[resource_class][index]
        match = self.matches.get(window.id)
        self.assertIsNotNone(match, f"{resource_class} unmatched: {self.unmatched.get(window.id)}")
        return self.frames[match.frame_index], match.rule

    def test_same_pid_apps(self):
        self.assertEqual(self.frame_for("org.kde.konsole")[0].name, "user : bash — Konsole")
        self.assertEqual(self.frame_for("Qemu-system-x86_64")[0].name, "QEMU (Example VM)")

    def test_system_settings_picks_main_frame_not_sidebar_or_offscreen(self):
        frame, _ = self.frame_for("systemsettings")
        self.assertEqual(frame.name, "Quick Settings  — System Settings")

    def test_flatpak_apps_match_by_caption_despite_pid_mismatch(self):
        frame, rule = self.frame_for("dev.deedles.Trayscale")
        self.assertEqual((frame.app_name, rule), ("trayscale", "sandbox+caption+size"))
        frame, rule = self.frame_for("org.mozilla.firefox")
        self.assertEqual(frame.app_name, "Firefox")

    def test_chromium_csd_matches_buffer_size(self):
        frame, rule = self.frame_for("com.anthropic.Claude")
        self.assertEqual((frame.name, frame.width), ("Claude", 1182))

    def test_steam_picks_the_one_window_sized_frame_among_13(self):
        frame, rule = self.frame_for("steam")
        self.assertEqual((frame.width, frame.height, rule), (1150, 690, "pid+size"))

    def test_plasmashell_panels_match_by_size(self):
        sizes = sorted((self.frame_for("plasmashell", i)[0].width, self.frame_for("plasmashell", i)[0].height)
                       for i in range(len(self.by_class["plasmashell"])))
        self.assertEqual(sizes, [(66, 193), (66, 616), (421, 56), (1164, 40), (1164, 727)])

    def test_every_frame_used_at_most_once(self):
        used = [m.frame_index for m in self.matches.values()]
        self.assertEqual(len(used), len(set(used)))


class AmbiguityTest(unittest.TestCase):
    def test_two_identical_frames_stay_unmatched(self):
        scene = scene_from_payload({"currentDesktop": "d", "windows": [
            {"id": "a", "pid": 5, "caption": "", "layer": 2,
             "frame": {"x": 0, "y": 0, "w": 100, "h": 100}}]})
        frames = [SimpleNamespace(pid=5, name="", width=100, height=100, showing=True),
                  SimpleNamespace(pid=5, name="", width=100, height=100, showing=True)]
        matches, unmatched = match_windows(scene.windows, frames)
        self.assertEqual(matches, {})
        self.assertIn("a", unmatched)


if __name__ == "__main__":
    unittest.main()


class PopoverTest(unittest.TestCase):
    """Test desktop 2026-11-12: GTK4 popovers and Chromium's open <select>
    are read into the main window (RawElement.popup) but shown by KWin as
    a popup window of their own."""

    def test_gtk_menu_items_inside_the_popup(self):
        # MenuButton menu: KWin popup 124,494 180.9x157.3; items placed
        # like the main window. A second popup group lies elsewhere.
        area = Rect(124, 494.27, 180.91, 157.27)
        groups = {"/menu": [Rect(124, 506.27, 169, 30), Rect(124, 536.27, 169, 30)],
                  "/other": [Rect(500, 100, 50, 20)]}
        self.assertEqual(pick_popover(area, groups, 4.0), "/menu")

    def test_chromium_select_options(self):
        # options in page DIP, already divided by the scale
        area = Rect(80, 367, 65.45, 72.73)
        groups = {"/240": [Rect(80, 367, 50.9, 23.6), Rect(80, 391, 50.9, 23.6), Rect(80, 415, 50.9, 24.5)]}
        self.assertEqual(pick_popover(area, groups, 4.0), "/240")

    def test_partly_outside_is_no_match(self):
        area = Rect(100, 100, 100, 100)
        groups = {"/a": [Rect(110, 110, 50, 20), Rect(110, 250, 50, 20)], "/empty": []}
        self.assertIsNone(pick_popover(area, groups, 4.0))

    def test_largest_fitting_group_wins(self):
        area = Rect(0, 0, 300, 300)
        groups = {"/small": [Rect(10, 10, 10, 10)], "/big": [Rect(10, 40, 10, 10), Rect(10, 60, 10, 10)]}
        self.assertEqual(pick_popover(area, groups, 4.0), "/big")
