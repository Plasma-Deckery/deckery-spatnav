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

import os
import sys
import time
from dataclasses import dataclass, field, replace

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from .eligibility import is_menu_item_candidate, is_relevant_element, suppress_container
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

# Web content. Its subtree is in CSS/DIP pixels, unlike the toolkit's own UI
# around it (placement.py rule "chromium-dip").
WEB_DOCUMENT_ROLE = "document web"

# Popup lists of a closed combo box: never on screen, yet Chromium reports
# the selected option of a closed <select> as SHOWING (test desktop
# 2026-11-12, inside a non-showing "menu").
CLOSED_COMBO_POPUP_ROLES = frozenset(("menu", "list", "list box"))


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
    # Inside web content (WEB_DOCUMENT_ROLE): rect and the last web_clips
    # clips are in the web's coordinate space, the clips before it are not.
    web: bool = False
    web_clips: int = 0


@dataclass
class ReadStats:
    nodes: int = 0
    pruned: int = 0
    ms: float = 0.0
    # (accessible, rect) of the first child of every clipping container:
    # moves whenever the container scrolls, even when none of its targets
    # are in view (a page scrolled past all its controls). For the
    # freshness poll.
    probes: list = field(default_factory=list)


def live_rect(acc):
    return _rect_of(acc)


def _rect_of(acc):
    r = AXComponent.get_rect(acc)
    return Rect(r.x, r.y, r.width, r.height)


def _same_rect(a, b, tolerance=1.0):
    return (abs(a.x - b.x) <= tolerance and abs(a.y - b.y) <= tolerance
            and abs(a.w - b.w) <= tolerance and abs(a.h - b.h) <= tolerance)


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

    def walk(node, clips, depth, target_ancestor=None, parent_role="", web_from=None):
        """Returns True if node or a descendant produced an element.
        target_ancestor: (role, rect) of the nearest ancestor that is itself
        a target (eligibility is known pre-order from role + states).
        web_from: number of clips outside the web document this node is in."""
        stats.nodes += 1
        role = AXObject.get_role_name(node) or ""
        if parent_role == "closed combo box" and role in CLOSED_COMBO_POPUP_ROLES:
            stats.pruned += 1
            return False
        if role == WEB_DOCUMENT_ROLE and web_from is None:
            web_from = len(clips)
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
        relevant = is_relevant_element(node, role, states)
        if relevant and rect is None:
            rect = _rect_of(node)
        produced = False
        if depth >= MAX_DEPTH or (prune_hidden and not showing and depth > 0):
            stats.pruned += depth < MAX_DEPTH
        elif role == "popup menu" and depth > 0:
            # A submenu is its own surface (own KWin popup window), read as
            # its own frame via popup tracking — not part of this one.
            return False
        elif role == "popup menu":
            produced = walk_menu(node, child_clips, depth)
        else:
            child_ancestor = (role, rect) if relevant else target_ancestor
            as_parent = role
            if role == "combo box" and not states.contains(Atspi.StateType.EXPANDED):
                as_parent = "closed combo box"
            probe = child_clips is not clips
            for child in AXObject.iter_children(node):
                if probe:
                    stats.probes.append((child, _rect_of(child)))
                    probe = False
                produced = walk(child, child_clips, depth + 1, child_ancestor,
                                as_parent, web_from) or produced
        if not relevant:
            return produced
        if produced and suppress_container(role, parent_role):
            return True
        if rect is None:
            rect = _rect_of(node)
        if rect.empty or not _inside(rect, clips):
            return produced
        name = AXObject.get_name(node) or ""
        if target_ancestor is not None and role == target_ancestor[0] and \
                (not name or _same_rect(rect, target_ancestor[1])):
            # One control, two targets — keep the outer one. Test desktop
            # 2026-11-11: System Settings theme tiles (unnamed radio button
            # inside radio button 'Breeze'); Plasma panel (button inside
            # button with the same rect: launcher, tray icons, clock).
            return produced
        out.append(RawElement(element_id(node), role, name, rect, clips, node,
                              web=web_from is not None,
                              web_clips=0 if web_from is None else len(clips) - web_from))
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
        menu_rect = _rect_of(menu)
        repaired = repair_menu_layout([(r, ok) for _, r, ok in children], menu_rect)
        if os.environ.get("DECKERY_DEBUG_MENUS"):
            print(f"[menu] {AXObject.get_name(menu)!r} {menu_rect} depth={depth}", file=sys.stderr)
            for index, (child, rect, showing) in enumerate(children):
                print(f"[menu]   {AXObject.get_role_name(child)} {AXObject.get_name(child)!r} {rect} "
                      f"showing={showing} repaired={repaired.get(index)}", file=sys.stderr)
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
    # Qt reports a submenu's WINDOW coordinates relative to its parent menu
    # (Dolphin's "Create New" opening leftwards: x=-316), while it is placed
    # on screen as its own popup surface: rebase onto the popup's own origin.
    if (AXObject.get_role_name(frame) or "") == "popup menu":
        base = _rect_of(frame)
        if base.x or base.y:
            out = [_rebase(e, base.x, base.y) for e in out]
    stats.ms = 1000 * (time.monotonic() - t0)
    return out, stats


def _rebase(element, dx, dy):
    def shift(r):
        return Rect(r.x - dx, r.y - dy, r.w, r.h)
    return replace(element, rect=shift(element.rect), clips=tuple(shift(c) for c in element.clips))
