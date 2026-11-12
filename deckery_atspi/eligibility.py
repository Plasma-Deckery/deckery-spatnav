"""Which AT-SPI objects are navigation targets.

Started as a copy of prototype_atspi_scan.is_relevant_element and its
container-suppression set (the old pipeline keeps using those unchanged);
v2-only changes are marked with the finding that motivated them.

Pure: works on already-fetched data (role name, the set of
Atspi.StateType values as ints), so the reader can feed it from libatspi
or from batched D-Bus replies alike."""

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

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

SHOWING = int(Atspi.StateType.SHOWING)
ENABLED = int(Atspi.StateType.ENABLED)
SENSITIVE = int(Atspi.StateType.SENSITIVE)
FOCUSABLE = int(Atspi.StateType.FOCUSABLE)
FOCUSED = int(Atspi.StateType.FOCUSED)
EXPANDED = int(Atspi.StateType.EXPANDED)


def states_from_bits(low, high):
    """GetState's two uint32 words → set of Atspi.StateType ints (bit n
    set = state n, as libatspi decodes it)."""
    bits = low | (high << 32)
    return frozenset(n for n in range(64) if bits >> n & 1)


def states_from_state_set(state_set):
    return frozenset(int(s) for s in state_set.get_states())


def _is_enabled(states):
    # Test desktop 2026-11-11, Flatseal (GTK4/libadwaita): GTK4 never sets
    # ENABLED, only SENSITIVE — every GTK4 widget looked disabled.
    return ENABLED in states or SENSITIVE in states


def is_relevant(role_name, states, get_name):
    """FOCUSABLE+SHOWING+ENABLED, with a role-based fallback for QML/
    Kirigami list rows (not focusable there) and Firefox's named menu-bar
    menus (not SHOWING while closed). get_name() is only called for the
    roles that need it (menu, canvas): every AT-SPI call is a round trip
    (~0.4 ms when made one at a time, profiled on the test desktop)."""
    if role_name in NEVER_RELEVANT_ROLES:
        return False
    is_showing = SHOWING in states
    is_enabled = _is_enabled(states)
    if role_name == "menu" and get_name():
        return is_enabled
    if not (is_showing and is_enabled):
        return False
    # Orca's AXUtilitiesState.is_focusable also counts FOCUSED.
    if FOCUSABLE in states or FOCUSED in states:
        return True
    # Test desktop 2026-11-11, Plasma desktop (FolderView): each desktop
    # icon is a named, non-focusable "canvas" whose only focusable child is
    # a label (and labels are never targets).
    if role_name == "canvas" and get_name():
        return True
    # v2, test desktop 2026-11-11: Qt menu items are SHOWING but never
    # FOCUSABLE (Dolphin context menu, menu bar submenus).
    return role_name in ("list item", "table cell", "tree item",
                         "menu item", "check menu item", "radio menu item")


MENU_ITEM_ROLES = frozenset(("menu item", "check menu item", "radio menu item"))


def is_menu_item_candidate(role_name, states, alive):
    """Eligible except for SHOWING — for menu_repair: Qt reports shared
    actions in an open menu as not showing."""
    if not alive:
        return False
    if role_name not in MENU_ITEM_ROLES:
        return False
    return _is_enabled(states)
