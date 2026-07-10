#!/usr/bin/env python3
"""
deckery_draw.py — cairo drawing for the spatial-nav overlay (deckery#28).

Pure rendering: given the current element list (and a HUD status line),
draws colored rectangles + labels. No AT-SPI, no D-Bus, no filtering logic
here — that all lives upstream in deckery_walk.py / deckery_filter.py /
deckery_tracker.py. Also owns the click-through input-region reset, since
that's a per-draw-call cairo/GDK surface concern.
"""

import cairo

# Rough role -> color bucket, just so different kinds of controls are
# visually distinguishable at a glance. Anything unlisted falls back to grey.
ROLE_COLORS = {
    "push button": (0.20, 0.80, 0.35, 0.45),
    "button": (0.20, 0.80, 0.35, 0.45),
    "push button menu": (0.20, 0.80, 0.35, 0.45),
    "radio button": (0.95, 0.60, 0.10, 0.45),
    "check box": (0.95, 0.60, 0.10, 0.45),
    "menu item": (0.35, 0.55, 0.95, 0.45),
    "list item": (0.85, 0.30, 0.75, 0.35),
    "entry": (0.95, 0.20, 0.20, 0.45),
    "text": (0.95, 0.20, 0.20, 0.45),
    "slider": (0.95, 0.60, 0.10, 0.45),
}
DEFAULT_COLOR = (0.7, 0.7, 0.7, 0.3)


def reset_click_through(surface):
    """Click-through: empty input region means the compositor never
    delivers pointer/click events to this surface — the desktop stays fully
    usable underneath. Must be re-applied on every draw since the Wayland
    surface can be recreated (e.g. on show/hide cycles)."""
    if surface:
        surface.set_input_region(cairo.Region())


def draw_elements(cr, elements):
    for depth, role, name, extents in elements:
        if extents is None:
            continue
        x, y, w, h = extents.x, extents.y, extents.width, extents.height
        if w <= 0 or h <= 0:
            continue

        r, g, b, a = ROLE_COLORS.get(role, DEFAULT_COLOR)
        cr.set_source_rgba(r, g, b, a)
        cr.set_line_width(2)
        cr.rectangle(x, y, w, h)
        cr.stroke()

        if name:
            cr.set_source_rgba(r, g, b, 1.0)
            cr.move_to(x + 2, max(y - 3, 10))
            cr.set_font_size(11)
            label = name if len(name) <= 30 else name[:27] + "..."
            cr.show_text(f"{role}: {label}")


def draw_hud(cr, text):
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.move_to(12, 20)
    cr.set_font_size(14)
    cr.show_text(text)
