"""Which part of each window the user can actually see.

Pure function of the Scene: walk the stack top → bottom, and every mapped
window covers everything below it within its frame. A window whose visible
region ends up empty is never read via AT-SPI at all."""

from .geometry import region_area, region_intersect, region_subtract
from .scene import LAYER_OVERLAY


def occludes(scene, window):
    """Whether this window hides what is under it. Overlay-layer surfaces
    are transparent, click-through layer-shell overlays (our own HUD/cursor
    overlay among them, confirmed live: python3.14 at layer 9 spanning the
    whole screen) — counting them would occlude everything."""
    return scene.is_mapped(window) and window.layer < LAYER_OVERLAY


def is_target_window(scene, window):
    return scene.is_mapped(window) and window.layer < LAYER_OVERLAY


def visible_regions(scene):
    """{window.id: [Rect, ...]} for every target window with a non-empty
    visible part, clipped to the union of outputs."""
    covered_above = []
    result = {}
    for window in reversed(scene.windows):
        if not is_target_window(scene, window):
            continue
        region = []
        for output in scene.outputs:
            cut = window.frame.intersect(output.geometry)
            if cut is not None:
                region.append(cut)
        for cover in covered_above:
            region = region_subtract(region, cover)
            if not region:
                break
        if region and region_area(region) > 0:
            result[window.id] = region
        if occludes(scene, window):
            covered_above.append(window.frame)
    return result


def visible_part(region, rect):
    """The sub-rects of `rect` that are visible within a window's region."""
    return region_intersect(region, rect)
