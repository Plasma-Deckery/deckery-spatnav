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


# Roles that are purely descriptive, never actionable — no click target, and
# since this overlay isn't a screen reader there's no speech-output reason to
# mark them either (deckery#28, user request: "da ich mit dem Label ja nicht
# interagieren kann ... brauche ich auch keine Labels"). Checked unconditionally
# in is_relevant_element(), ahead of the FOCUSABLE check, because a handful of
# toolkits do mark static labels FOCUSABLE (e.g. for screen-reader tab order)
# even though there's nothing to do with them here.
_NEVER_RELEVANT_ROLES = frozenset(("label",))

# Structural container roles that get suppressed when at least one of their
# OWN descendants is independently relevant (deckery#28, user request): the
# container's box would just be a redundant rectangle-around-rectangles once
# its actionable content is already drawn as separate boxes — e.g. a
# "page tab list" wrapping individually-clickable "page tab" entries, or a
# "scroll pane" wrapping the actual scrollable content. Still drawn if it has
# NO eligible descendant (e.g. an empty list) — the container itself may be
# the only thing worth marking in that case. Deliberately excludes
# window-level roles (FRAME, DIALOG, WINDOW) per user request — those stay
# visible regardless of descendants.
_SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES = frozenset((
    "page tab list",
    "scroll pane",
    "panel",
    "filler",
    "section",
    "list",
    "table",
    "tree",
    "menu bar",
))


def is_relevant_element(node):
    """Returns True if node should be included in the spatial-nav overlay.

    Checks FOCUSABLE+SHOWING+ENABLED, with a role-based fallback for QML/
    Kirigami list rows — see walk_elements() docstring for the full rationale.
    Used both by the initial walk and by incremental event handlers so the
    eligibility logic lives in exactly one place."""
    if AXObject.is_dead(node):
        return False
    role_name = AXObject.get_role_name(node) or ""
    if role_name in _NEVER_RELEVANT_ROLES:
        return False
    state_set = AXObject.get_state_set(node)
    is_showing = AXUtilitiesState.is_showing(node, state_set)
    is_enabled = AXUtilitiesState.is_enabled(node, state_set)
    # Firefox menu bar items (File, Edit, View) have role "menu" and report
    # is_showing=False even when the bar is visible — include them when they
    # have a name. Unnamed "menu" nodes are hidden dropdown/context menus
    # pre-built by Firefox; those become showing=True when actually opened
    # and are caught by on_state_changed() at that point.
    if role_name == "menu" and (AXObject.get_name(node) or ""):
        return is_enabled
    if not (is_showing and is_enabled):
        return False
    if AXUtilitiesState.is_focusable(node, state_set):
        return True
    # QML/Kirigami list rows (e.g. systemsettings category sidebar) are never
    # individually FOCUSABLE — only their parent list container gets that state.
    # The rows are navigated via arrow keys inside the list, not by per-row OS
    # keyboard focus. Accept list item / table cell / tree item roles that are
    # showing+enabled as a fallback so we draw one box per selectable row
    # rather than one big box around the entire list (deckery#28).
    return role_name in ("list item", "table cell", "tree item")


def walk_elements(node, max_depth=25, _depth=0, results=None):
    """Walks the AT-SPI tree under node and returns a list of
    Atspi.Accessible objects that pass is_relevant_element(), minus
    structural containers subsumed by their own eligible descendants (see
    _SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES above).

    Orca-style: stores the actual accessible objects rather than
    pre-extracting (role, name, extents) tuples. Callers obtain attributes
    on demand via AXObject / AXComponent — those calls are served from
    Orca's AXCacheManager after the first walk, so subsequent renders and
    incremental updates are cheap.

    Note on CoordType: AXComponent.get_rect() hardcodes CoordType.WINDOW
    because on this system (KDE/Qt AT-SPI bridge, Scale 1.1 + software-
    rotated panel) CoordType.SCREEN silently returns the same window-
    relative numbers anyway. The real screen offset is added back at render
    time from the KWin tracker's clientGeometry (deckery#28)."""
    if results is None:
        results = []
    if _depth > max_depth:
        return results
    # Post-order: recurse into children FIRST, so we know whether this node
    # already has an independently-eligible descendant before deciding
    # whether to add its own (possibly now-redundant) box.
    before_count = len(results)
    for child in AXObject.iter_children(node):
        walk_elements(child, max_depth, _depth + 1, results)
    has_eligible_descendant = len(results) > before_count

    if is_relevant_element(node):
        role_name = AXObject.get_role_name(node) or ""
        suppress = (
            has_eligible_descendant
            and role_name in _SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES
        )
        if not suppress:
            results.append(node)
    return results


def walk(node, depth=0, max_depth=25, results=None):
    """Legacy tuple-returning walk, kept for the standalone main() below.
    New code should use walk_elements() instead."""
    if results is None:
        results = []
    if depth > max_depth:
        return results
    if is_relevant_element(node):
        rect = AXComponent.get_rect(node)
        extents = rect if rect.width or rect.height or rect.x or rect.y else None
        results.append((depth, AXObject.get_role_name(node) or "", AXObject.get_name(node) or "", extents))
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
