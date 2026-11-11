"""Reads the target elements of one window from AT-SPI.

Walks the matched frame post-order (children first, so a container can be
dropped when it has eligible descendants — prototype_atspi_scan's
suppression rule). Output is window-relative: placing on screen and
clipping against other windows happens afterwards in compose.py, so moving
or restacking windows never needs AT-SPI.

Clipping containers (scroll panes, lists, …) are applied here because they
are window-internal: a subtree whose clipping container lies entirely
outside its own clipping ancestors is not walked at all — for long lists,
that skips every scrolled-away row."""

import time
from dataclasses import dataclass, field

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from .eligibility import (SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES, is_menu_item_candidate,
                          is_relevant_element)
from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject
from vendor.orca_ax.ax_utilities_state import AXUtilitiesState

from .atspi_frames import element_id
from .geometry import Rect
from .menu_repair import repair_menu_layout

# Ancestors that clip their descendants to their own bounds. Deliberately a
# role list rather than "every ancestor": some toolkits report bogus extents
# for plain containers (filler/panel 0x0 or stale), which would wrongly cut
# away real elements.
CLIPPING_ROLES = frozenset((
    "scroll pane", "viewport", "list", "list box", "tree", "tree table", "table",
    "document frame", "document web", "page tab list",
))

MAX_DEPTH = 40


@dataclass(frozen=True)
class RawElement:
    id: str
    role: str
    name: str
    rect: Rect     # window-relative, as AT-SPI reports it
    clips: tuple   # window-relative rects of clipping ancestors, outermost first
    accessible: object = field(default=None, compare=False, repr=False)
    # Geometry reconstructed by menu_repair, not reported by AT-SPI: the
    # live rect will keep disagreeing, so freshness polling must skip it.
    inferred: bool = False


@dataclass
class ReadStats:
    nodes: int = 0
    pruned: int = 0
    ms: float = 0.0


def live_rect(acc):
    return _rect_of(acc)


def _rect_of(acc):
    r = AXComponent.get_rect(acc)
    return Rect(r.x, r.y, r.width, r.height)


def _inside(rect, clips):
    for c in clips:
        rect = rect.intersect(c)
        if rect is None:
            return False
    return True


# Toolkits whose SHOWING state is trustworthy on containers, so a subtree
# under a non-showing node can be skipped without looking inside: Qt's
# closed menu-bar menus alone are hundreds of nodes in Dolphin. Not
# assumed for other toolkits yet (Firefox reports visible toolbar buttons
# as not showing, see ATSPI_LIMITATIONS.md).
PRUNE_NOT_SHOWING_TOOLKITS = frozenset(("qt",))


def read_window(frame, toolkit=""):
    stats = ReadStats()
    prune_hidden = (toolkit or "").lower() in PRUNE_NOT_SHOWING_TOOLKITS
    t0 = time.monotonic()
    out = []

    def walk(node, clips, depth):
        """Returns True if node or a descendant produced an element."""
        stats.nodes += 1
        role = AXObject.get_role_name(node) or ""
        states = AXObject.get_state_set(node)
        showing = AXUtilitiesState.is_showing(node, states)
        rect = None
        child_clips = clips
        if role in CLIPPING_ROLES:
            rect = _rect_of(node)
            if not rect.empty:
                if not _inside(rect, clips):
                    stats.pruned += 1
                    return False
                child_clips = clips + (rect,)
        produced = False
        if depth >= MAX_DEPTH or (prune_hidden and not showing and depth > 0):
            stats.pruned += depth < MAX_DEPTH
        elif role == "popup menu":
            produced = walk_menu(node, child_clips, depth)
        else:
            for child in AXObject.iter_children(node):
                produced = walk(child, child_clips, depth + 1) or produced
        if not is_relevant_element(node, role, states):
            return produced
        if produced and role in SUPPRESS_CONTAINER_IF_HAS_ELIGIBLE_CHILD_ROLES:
            return True
        if rect is None:
            rect = _rect_of(node)
        if rect.empty or not _inside(rect, clips):
            return produced
        out.append(RawElement(element_id(node), role, AXObject.get_name(node) or "", rect, clips, node))
        return True

    def walk_menu(menu, clips, depth):
        """Children of a popup menu, with menu_repair for items Qt reports
        as not showing with another menu's geometry."""
        children = []
        for child in AXObject.iter_children(menu):
            rect = _rect_of(child)
            showing = AXUtilitiesState.is_showing(child, AXObject.get_state_set(child))
            if showing or not rect.empty:
                children.append((child, rect, showing))
        repaired = repair_menu_layout([(r, ok) for _, r, ok in children], _rect_of(menu))
        produced = False
        for index, (child, rect, showing) in enumerate(children):
            if index in repaired and is_menu_item_candidate(child):
                stats.nodes += 1
                out.append(RawElement(element_id(child), AXObject.get_role_name(child) or "",
                                      AXObject.get_name(child) or "", repaired[index], clips, child,
                                      inferred=True))
                produced = True
            else:
                produced = walk(child, clips, depth + 1) or produced
        return produced

    walk(frame, (), 0)
    stats.ms = 1000 * (time.monotonic() - t0)
    return out, stats
