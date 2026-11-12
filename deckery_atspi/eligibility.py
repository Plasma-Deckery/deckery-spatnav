"""Which AT-SPI objects are navigation targets.

Started as a copy of prototype_atspi_scan.is_relevant_element and its
container-suppression set (the old pipeline keeps using those unchanged);
v2-only changes are marked with the finding that motivated them."""

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState

NEVER_RELEVANT_ROLES = frozenset(("label",))

# Dropped when they have an eligible descendant: the descendants are the
# real targets, the container would just be a huge box around them.
SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES = frozenset((
    "page tab list",
    "scroll pane",
    "panel",
    "filler",
    "section",
    "list",
    "table",
    "tree",
    "menu bar",
    # v2, test desktop 2026-11-11: Kirigami pages in System Settings are
    # focusable "page tab"s spanning the whole sidebar / content area.
    "page tab",
    # v2, test desktop 2026-11-12, Chromium: the page is focusable and
    # stays a target only when nothing in it is.
    "document web",
))


def suppress_container(role_name, parent_role):
    """A container with an eligible descendant is dropped — except a real
    tab in a tab strip (test desktop 2026-11-12, Chromium: each tab is a
    "page tab" in a "page tab list" with its own Close button inside; the
    Kirigami pages above sit in a panel/filler)."""
    if role_name == "page tab" and parent_role == "page tab list":
        return False
    return role_name in SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES


def _is_enabled(node, state_set):
    # Test desktop 2026-11-11, Flatseal (GTK4/libadwaita): GTK4 never sets
    # ENABLED, only SENSITIVE — every GTK4 widget looked disabled.
    return (AXUtilitiesState.is_enabled(node, state_set)
            or state_set.contains(Atspi.StateType.SENSITIVE))


def is_relevant_element(node, role_name=None, state_set=None):
    """FOCUSABLE+SHOWING+ENABLED, with a role-based fallback for QML/
    Kirigami list rows (not focusable there) and Firefox's named menu-bar
    menus (not SHOWING while closed). role_name/state_set can be passed in
    when the caller already has them: every AT-SPI call is a ~0.4 ms D-Bus
    round trip (profiled on the test desktop, Dolphin: the role was fetched
    twice per node and AXObject.is_dead() pinged every node)."""
    if role_name is None:
        role_name = AXObject.get_role_name(node) or ""
    if role_name in NEVER_RELEVANT_ROLES:
        return False
    if state_set is None:
        state_set = AXObject.get_state_set(node)
    is_showing = AXUtilitiesState.is_showing(node, state_set)
    is_enabled = _is_enabled(node, state_set)
    if role_name == "menu" and (AXObject.get_name(node) or ""):
        return is_enabled
    if not (is_showing and is_enabled):
        return False
    if AXUtilitiesState.is_focusable(node, state_set):
        return True
    # Test desktop 2026-11-11, Plasma desktop (FolderView): each desktop
    # icon is a named, non-focusable "canvas" whose only focusable child is
    # a label (and labels are never targets).
    if role_name == "canvas" and AXObject.get_name(node):
        return True
    # v2, test desktop 2026-11-11: Qt menu items are SHOWING but never
    # FOCUSABLE (Dolphin context menu, menu bar submenus).
    return role_name in ("list item", "table cell", "tree item",
                         "menu item", "check menu item", "radio menu item")


MENU_ITEM_ROLES = frozenset(("menu item", "check menu item", "radio menu item"))


def is_menu_item_candidate(node):
    """Eligible except for SHOWING — for menu_repair: Qt reports shared
    actions in an open menu as not showing."""
    if AXObject.is_dead(node):
        return False
    if (AXObject.get_role_name(node) or "") not in MENU_ITEM_ROLES:
        return False
    return _is_enabled(node, AXObject.get_state_set(node))
