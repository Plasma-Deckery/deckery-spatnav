#!/usr/bin/env python3
"""
prototype_atspi_scan.py — Standalone prototype, run inside the deckery
distrobox container (Gtk4LayerShell + Atspi GI bindings live there).

Walks the AT-SPI2 accessible tree for the currently focused/active
application window and prints every FOCUSABLE element with its screen
extents. Validates the tree-walk + extents API before building the
overlay renderer.

As of deckery#28's Orca-vendoring step, the actual AT-SPI calls go through
vendor/orca_ax's AXObject/AXComponent/AXUtilitiesState (see
vendor/orca_ax/README.md) instead of raw node.get_*()/get_component_iface()
calls. That buys us the same defensive exception handling + dead-object
tracking Orca itself relies on, instead of the ad-hoc bare try/except this
file used before — same behavior, hardened implementation.

Run:
    distrobox enter deckery -- python3 prototype_atspi_scan.py
"""

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState


def walk(node, depth=0, max_depth=25, results=None):
    if results is None:
        results = []
    if depth > max_depth:
        return results

    state_set = AXObject.get_state_set(node)
    is_focusable = AXUtilitiesState.is_focusable(node, state_set)
    is_showing = AXUtilitiesState.is_showing(node, state_set)
    is_enabled = AXUtilitiesState.is_enabled(node, state_set)

    # QML/Kirigami list rows (e.g. the systemsettings category sidebar) are
    # never individually FOCUSABLE — only their parent "list" container gets
    # that state. The rows themselves are navigated via an internal
    # "current item" highlight driven by arrow keys inside the list, not by
    # per-row OS keyboard focus. Confirmed via direct AT-SPI inspection
    # (deckery#28): systemsettings' 29-item sidebar list has FOCUSABLE only
    # on the "list" node; its "list item" children are
    # SHOWING+ENABLED+Action-capable but FOCUSABLE=False. Without this
    # role-based fallback, the overlay would draw a single box around the
    # entire list instead of one box per selectable category — so we also
    # accept "list item"/"table cell"/"tree item" roles that are showing+
    # enabled even without FOCUSABLE. This does NOT relax the check for
    # "heading"/other non-interactive roles, which stay FOCUSABLE-gated.
    role_name = AXObject.get_role_name(node) or ""
    is_implicit_row = role_name in ("list item", "table cell", "tree item")

    if (is_focusable or is_implicit_row) and is_showing and is_enabled:
        # Deliberately WINDOW, not SCREEN: on this system (KDE/Qt AT-SPI
        # bridge, display with Scale 1.1 + Rotation 8 software-rotated
        # panel) CoordType.SCREEN does NOT apply the compositor's
        # rotation/scale transform and returns the same numbers as
        # WINDOW — i.e. it's silently window-client-relative, not
        # screen-absolute. Confirmed by comparing a window's own AT-SPI
        # frame extents against KWin's real clientGeometry for it (sizes
        # matched, only the absolute offset was missing). AXComponent.get_rect()
        # already hardcodes CoordType.WINDOW for this exact reason (see
        # vendor/orca_ax/ax_component.py); the caller
        # (prototype_atspi_overlay.py) adds the real screen offset back in
        # using the KWin window-tracker's clientGeometry for the matching
        # window (deckery#28).
        rect = AXComponent.get_rect(node)
        extents = rect if rect.width or rect.height or rect.x or rect.y else None

        name = AXObject.get_name(node) or ""
        role = role_name

        results.append((depth, role, name, extents))

    for child in AXObject.iter_children(node):
        walk(child, depth + 1, max_depth, results)

    return results


def main():
    desktop = Atspi.get_desktop(0)
    n_apps = desktop.get_child_count()
    print(f"Desktop has {n_apps} top-level app(s) registered with AT-SPI2.\n")

    total = 0
    for i in range(n_apps):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            app_name = app.get_name() or "(unnamed)"
        except Exception:
            app_name = "(unnamed)"

        results = walk(app)
        if not results:
            continue

        print(f"=== {app_name} — {len(results)} focusable element(s) ===")
        for depth, role, name, extents in results[:30]:  # cap per-app for readability
            indent = "  " * depth
            if extents:
                box = f"x={extents.x} y={extents.y} w={extents.width} h={extents.height}"
            else:
                box = "no extents"
            print(f"{indent}[{role}] '{name}'  {box}")
        if len(results) > 30:
            print(f"  ... and {len(results) - 30} more")
        print()
        total += len(results)

    print(f"Total focusable elements across all apps: {total}")


if __name__ == "__main__":
    main()
