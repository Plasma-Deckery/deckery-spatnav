import unittest

from deckery_atspi.geometry import Rect
from deckery_atspi.menu_repair import repair_menu_layout


def R(x, y, w, h):
    return Rect(x, y, w, h)


# Dolphin context menu on the test desktop, 2026-11-11 (popup 373x623).
# (rect as reported, SHOWING) in child order; the first four children were
# None in this dump and are left out, so "Open Folder With" comes first.
DOLPHIN = [
    (R(4, 94, 365, 30), True),    # Open Folder With
    (R(4, 124, 365, 5), True),    # separator
    (R(4, 129, 365, 30), True),   # Create New
    (R(4, 159, 365, 5), True),    # separator
    (R(4, 69, 338, 30), False),   # Cut            (Edit menu geometry)
    (R(4, 99, 338, 30), False),   # Copy
    (R(4, 129, 338, 30), False),  # Copy Location
    (R(4, 194, 256, 30), False),  # Duplicate Here
    (R(4, 164, 256, 30), False),  # Rename…
    (R(4, 129, 256, 30), False),  # Add to Places
    (R(4, 344, 365, 5), True),    # separator
    (R(4, 349, 365, 30), True),   # Move to Trash
    (R(4, 379, 365, 5), True),    # separator
    (R(4, 64, 321, 30), False),   # Open Terminal Here
    (R(4, 414, 365, 30), True),   # View Disk Usage Statistics
    (R(4, 444, 365, 30), True),   # Compress
    (R(4, 474, 365, 30), True),   # Activities
    (R(4, 504, 365, 50), True),   # tag colour row
    (R(4, 554, 365, 30), True),   # Assign Tags
    (R(4, 584, 365, 5), True),    # separator
    (R(4, 324, 256, 30), False),  # Properties
]


class DolphinContextMenuTest(unittest.TestCase):
    def setUp(self):
        self.repaired = repair_menu_layout(DOLPHIN, R(0, 0, 373, 623))

    def test_shared_actions_fill_the_gap_in_30px_slots(self):
        ys = [self.repaired[i].y for i in range(4, 10)]
        self.assertEqual(ys, [164, 194, 224, 254, 284, 314])
        self.assertTrue(all(self.repaired[i].h == 30 for i in range(4, 10)))
        self.assertTrue(all((self.repaired[i].x, self.repaired[i].w) == (4, 365) for i in range(4, 10)))

    def test_single_broken_item_between_separator_and_item(self):
        self.assertEqual(self.repaired[13], R(4, 384, 365, 30))  # Open Terminal Here

    def test_trailing_broken_item_runs_to_menu_bottom_margin(self):
        # Properties: after the last separator (589) down to 623 - 4 margin.
        self.assertEqual((self.repaired[20].y, self.repaired[20].y2), (589, 619))

    def test_gap_that_does_not_fit_is_not_guessed(self):
        # one broken item in a 90 px gap: 3x the typical height → no repair
        entries = [(R(4, 4, 300, 30), True), (R(4, 0, 300, 30), False), (R(4, 124, 300, 30), True)]
        self.assertEqual(repair_menu_layout(entries, R(0, 0, 308, 158)), {})

    def test_all_broken_but_consistent_keeps_rows_takes_width_from_menu(self):
        entries = [(R(4, 4, 281, 30), False), (R(4, 34, 281, 5), False), (R(4, 39, 281, 30), False)]
        repaired = repair_menu_layout(entries, R(0, 0, 320, 318))
        self.assertEqual(repaired[2], R(4, 39, 312, 30))

    def test_all_broken_overlapping_is_not_guessed(self):
        entries = [(R(4, 64, 281, 30), False), (R(4, 34, 281, 30), False)]
        self.assertEqual(repair_menu_layout(entries, R(0, 0, 320, 318)), {})

    def test_intact_entries_untouched(self):
        self.assertTrue(all(DOLPHIN[i][1] for i in range(len(DOLPHIN)) if i not in self.repaired))


if __name__ == "__main__":
    unittest.main()
