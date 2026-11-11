"""Reconstructs menu item geometry Qt reports wrongly (pure).

Qt bug found on the test desktop (2026-11-11, Dolphin 25.x context menu):
a QAction that also lives in another menu (Cut, Copy, Rename…, Properties,
Open Terminal Here — shared with the Edit/File menus) is reported with
SHOWING=False and the geometry it has in that OTHER menu ("Cut" at y=69,
width 338 = its slot in the Edit menu), while it is plainly visible in the
open context menu.

The menu's children are still in visual order, and the correctly reported
neighbours (items and separators) pin down the gap the broken run has to
fill: between a separator ending at y=164 and one starting at y=344 sit
exactly six broken items → 30 px each, matching every intact item."""

from .geometry import Rect


# A reconstructed slot may differ this much from the menu's typical item
# height; beyond that the gap doesn't fit the run (e.g. a genuinely hidden
# action in it) and nothing is guessed.
HEIGHT_TOLERANCE = 0.2


def repair_menu_layout(entries, menu_rect):
    """entries: list of (rect, ok) in child order, rects in the same space
    as menu_rect, ok = reported correctly (SHOWING). Children that are
    neither showing nor have any geometry (hidden title items) must be left
    out by the caller. Returns {index: Rect} for broken entries whose slot
    can be reconstructed: a run of broken entries between two ok neighbours
    (or the menu edge) is spread evenly over the gap, using the ok
    neighbours' x/width."""
    repaired = {}
    anchors = [i for i, (rect, ok) in enumerate(entries) if ok and not rect.empty]
    if not anchors:
        return repaired
    # Menus pad their items equally on all sides; the left inset of an
    # intact item stands in for the top/bottom inset at the menu edges.
    margin = entries[anchors[0]][0].x - menu_rect.x
    heights = sorted(entries[a][0].h for a in anchors if entries[a][0].h > 10)
    typical = heights[len(heights) // 2] if heights else None
    i = 0
    while i < len(entries):
        rect, ok = entries[i]
        if ok:
            i += 1
            continue
        start = i
        while i < len(entries) and not entries[i][1]:
            i += 1
        run = range(start, i)
        above = next((a for a in reversed(anchors) if a < start), None)
        below = next((a for a in anchors if a >= i), None)
        top = entries[above][0].y2 if above is not None else menu_rect.y + margin
        bottom = entries[below][0].y if below is not None else menu_rect.y2 - margin
        if bottom <= top:
            continue
        height = (bottom - top) / len(run)
        if typical is None or abs(height - typical) > HEIGHT_TOLERANCE * typical:
            continue
        x_ref = entries[above][0] if above is not None else entries[below][0]
        for k, index in enumerate(run):
            repaired[index] = Rect(x_ref.x, top + k * height, x_ref.w, height)
    return repaired
