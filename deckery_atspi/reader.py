"""Reads the target elements of one window from AT-SPI.

Walks the matched frame post-order (children first, so a container can be
dropped when it has eligible descendants — prototype_atspi_scan's
suppression rule). Output is window-relative: placing on screen and
clipping against other windows happens afterwards in compose.py, so moving
or restacking windows never needs AT-SPI.

Clipping containers (scroll panes, lists, …) are applied here because they
are window-internal: a subtree whose clipping container lies entirely
outside its own clipping ancestors is not walked at all — for long lists,
that skips every scrolled-away row.

Two sources feed the same walk (select_targets):
- default: the subtree is fetched first with pipelined D-Bus calls
  (atspi_bus.py) — every node of the frontier asked at once, and only
  what the walk will look at (fetch_tree);
- DECKERY_READER=sync: libatspi, one blocking call per property
  (LiveNode) — the pre-2026-11-12 reader, kept as fallback when the
  private bus connection can't be made.
DECKERY_READER=compare reads every window both ways and logs differences."""

import functools
import os
import sys
import time
from dataclasses import dataclass, field, replace

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from vendor.orca_ax.ax_component import AXComponent
from vendor.orca_ax.ax_object import AXObject

from .atspi_bus import ACCESSIBLE, COMPONENT, COORD_TYPE_WINDOW, NULL_PATH, PROPERTIES, shared_bus
from .atspi_frames import element_id
from .eligibility import (EXPANDED, FOCUSABLE, SHOWING, VISIBLE, is_enabled, is_menu_item_candidate, is_relevant,
                          states_from_bits, states_from_state_set, suppress_container)
from .geometry import Rect
from .menu_repair import repair_menu_layout

# Ancestors that clip their descendants to their own bounds. Deliberately a
# role list rather than "every ancestor": some toolkits report bogus extents
# for plain containers (filler/panel 0x0 or stale), which would wrongly cut
# away real elements.
CLIPPING_ROLES = frozenset((
    "scroll pane", "viewport", "list", "list box", "tree", "tree table", "table",
    "document frame", "document web", "page tab list",
    # Also clips nothing as such — listed for COLLAPSED_IF_EMPTY_ROLES.
    "tool bar",
))
# Containers whose children are off screen when they report no size.
# Test desktop 2026-11-12, Firefox: the auto-hidden menu bar's tool bar
# reports (-1,-1,-1,-1) while its Close button keeps SHOWING with a rect
# poking out above the window.
COLLAPSED_IF_EMPTY_ROLES = frozenset(("tool bar",))

MAX_DEPTH = 40

# Web content. Its subtree is in CSS/DIP pixels, unlike the toolkit's own UI
# around it (placement.py rule "chromium-dip").
WEB_DOCUMENT_ROLE = "document web"

# Popup lists of a closed combo box: never on screen, yet Chromium reports
# the selected option of a closed <select> as SHOWING (test desktop
# 2026-11-12, inside a non-showing "menu").
CLOSED_COMBO_POPUP_ROLES = frozenset(("menu", "list", "list box"))

# One control drawn as several accessibles: everything a combo box has
# inside its own box is its rendering (test desktop 2026-11-12,
# gtk_testapp: a GTK4 DropDown is a combo box around a toggle button
# around a focusable, unnamed panel); only what lies outside — its popup
# list — is walked.
COMBO_BOX_ROLE = "combo box"

READER_MODE = os.environ.get("DECKERY_READER", "async")


@dataclass(frozen=True)
class RawElement:
    id: str
    role: str
    name: str
    rect: Rect     # window-relative, as AT-SPI reports it
    clips: tuple   # window-relative rects of clipping ancestors, outermost first
    # Atspi.Accessible (sync reader) or AccessibleRef (fetched): see .accessible
    ref: object = field(default=None, compare=False, repr=False)
    # Geometry reconstructed by menu_repair, not reported by AT-SPI: the
    # live rect will keep disagreeing, so freshness polling must skip it.
    inferred: bool = False
    # Inside web content (WEB_DOCUMENT_ROLE): rect and the last web_clips
    # clips are in the web's coordinate space, the clips before it are not.
    web: bool = False
    web_clips: int = 0
    # Inside the popup of an EXPANDED node (a child of it lying outside
    # it, or a combo box's list): that child's id. Test desktop
    # 2026-11-12: GTK4 popovers (menu button, drop-down) and Chromium's
    # <select> list are subtrees of the main window, in its coordinates,
    # while KWin shows them as their own popup window
    # (backend._match_popovers).
    popup: str = ""

    @property
    def accessible(self):
        """The Atspi.Accessible, resolved on first use for fetched
        elements (None if the object went away)."""
        ref = self.ref
        return ref.resolve() if isinstance(ref, AccessibleRef) else ref


@dataclass(frozen=True)
class Probe:
    """First child of a clipping container, as read: moves whenever the
    container scrolls, even when none of its targets are in view (a page
    scrolled past all its controls). For the freshness poll."""
    id: str
    rect: Rect
    ref: object = field(default=None, compare=False, repr=False)
    accessible = RawElement.accessible


@dataclass
class ReadStats:
    nodes: int = 0
    pruned: int = 0
    ms: float = 0.0
    probes: list = field(default_factory=list)   # [Probe], one per clipping container
    calls: int = 0      # D-Bus calls of the fetch (0 for the sync reader)
    # Values the walk needed that the fetch had not asked for (each one a
    # blocking call) — should stay 0, else fetch_tree misses a rule.
    fallbacks: int = 0
    note: str = ""      # DECKERY_READER=compare: sync timing / difference


def live_rect(acc):
    return _rect_of(acc)


def _rect_of(acc):
    r = AXComponent.get_rect(acc)
    return Rect(r.x, r.y, r.width, r.height)


def _same_rect(a, b, tolerance=1.0):
    return (abs(a.x - b.x) <= tolerance and abs(a.y - b.y) <= tolerance
            and abs(a.w - b.w) <= tolerance and abs(a.h - b.h) <= tolerance)


def _contains(outer, inner, tolerance=1.0):
    return (inner.x >= outer.x - tolerance and inner.y >= outer.y - tolerance
            and inner.x2 <= outer.x2 + tolerance and inner.y2 <= outer.y2 + tolerance)


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
# Toolkits that report SENSITIVE per widget, not inherited from an
# insensitive container: everything under a node that is neither ENABLED
# nor SENSITIVE is disabled. Test desktop 2026-11-12, gtk_testapp: the
# button in an insensitive Gtk.Frame is SENSITIVE, only the frame's
# "grouping" is not. Not assumed elsewhere yet: other toolkits weren't
# checked for containers without either state.
INHERIT_DISABLED_TOOLKITS = frozenset(("gtk",))


# ---- top-down rules, shared by the walk and fetch_tree ----
#
# fetch_tree has to decide which children to fetch before the walk runs;
# both go through these two functions, so they can't disagree.

LEAF, SUBMENU, MENU, CHILDREN = "leaf", "submenu", "menu", "children"


def _child_clips(role, rect, clips):
    """For a clipping container: the clips its children are cut to, or
    None if it lies entirely outside its own clipping ancestors (whole
    subtree skipped). An empty rect clips nothing, except for a collapsed
    container."""
    if rect.empty:
        return None if role in COLLAPSED_IF_EMPTY_ROLES else clips
    if not _inside(rect, clips):
        return None
    return clips + (rect,)


def _closed_combo_popup(role, parent_role):
    """Not walked at all (before even its states are looked at)."""
    return parent_role == "closed combo box" and role in CLOSED_COMBO_POPUP_ROLES


def _role_for_children(role, states):
    """The parent_role a node's children are walked with."""
    if role == "combo box" and EXPANDED not in states:
        return "closed combo box"
    return role


def _descent(role, states, depth, prune_hidden):
    showing = SHOWING in states
    if depth >= MAX_DEPTH or (depth > 0 and not showing and (prune_hidden or VISIBLE not in states)):
        # Neither SHOWING nor VISIBLE: off screen in every toolkit. Test
        # desktop 2026-11-12, Firefox: a closed context menu stays in the
        # browser frame's tree with its old rect, its items still SHOWING.
        return LEAF
    if role == "popup menu":
        # A submenu is its own surface (own KWin popup window), read as
        # its own frame via popup tracking — not part of this one.
        return SUBMENU if depth > 0 else MENU
    return CHILDREN


# ---- the walk ----

def _children_rect_needed(role, states):
    """Whether a node's children are each placed against its rect: the
    combo box rule and popups (RawElement.popup)."""
    return role == COMBO_BOX_ROLE or EXPANDED in states


def select_targets(root, prune_hidden, stats, rebase=False, inherit_disabled=False):
    """The targets under root. Nodes provide id, role, states (set of
    Atspi.StateType ints), rect, name, children, alive and ref; the walk
    reads each only where it needs it (a LiveNode turns every first read
    into one libatspi call)."""
    out = []

    def walk(node, clips, depth, target_ancestor=None, parent_role="", web_from=None, popup="",
             disabled=False):
        """Returns True if node or a descendant produced an element.
        target_ancestor: _Ancestor, the nearest ancestor that is itself a
        target (eligibility is known pre-order from role + states).
        web_from: number of clips outside the web document this node is in.
        popup: id of the popup candidate this node is in.
        disabled: under a disabled container (inherit_disabled)."""
        stats.nodes += 1
        role = node.role
        if _closed_combo_popup(role, parent_role):
            stats.pruned += 1
            return False
        if role == WEB_DOCUMENT_ROLE and web_from is None:
            web_from = len(clips)
        states = node.states
        rect = None
        child_clips = clips
        if role in CLIPPING_ROLES:
            rect = node.rect
            child_clips = _child_clips(role, rect, clips)
            if child_clips is None:
                stats.pruned += 1
                return False
        # The root (window, popup) is never a target itself: test desktop
        # 2026-11-12, a Firefox context menu ("menu", SHOWING) became one
        # box around its own items.
        relevant = depth > 0 and not disabled and is_relevant(role, states, lambda: node.name)
        place_children = _children_rect_needed(role, states)
        if (relevant or place_children) and rect is None:
            rect = node.rect
        produced = False
        descent = _descent(role, states, depth, prune_hidden)
        if descent is LEAF:
            stats.pruned += depth < MAX_DEPTH
        elif descent is SUBMENU:
            return False
        elif descent is MENU:
            produced = walk_menu(node, child_clips, depth)
        else:
            own = _Ancestor(role, rect, node.name, FOCUSABLE in states) if relevant else None
            child_ancestor = own or target_ancestor
            as_parent = _role_for_children(role, states)
            probe = child_clips is not clips
            child_disabled = disabled or (inherit_disabled and depth > 0 and not is_enabled(states))
            for child in node.children:
                if probe:
                    stats.probes.append(Probe(child.id, child.rect, child.ref))
                    probe = False
                child_popup = popup
                if place_children:
                    inside = child.rect.empty or _contains(rect, child.rect)
                    # Chromium: the open <select>'s "menu" has a bogus
                    # 72x11 rect inside the combo box, its items lie below.
                    if EXPANDED in states and (not inside or role == COMBO_BOX_ROLE
                                               and child.role in CLOSED_COMBO_POPUP_ROLES):
                        child_popup = child.id
                    elif role == COMBO_BOX_ROLE and inside:
                        stats.pruned += 1
                        continue
                produced = walk(child, child_clips, depth + 1, child_ancestor,
                                as_parent, web_from, child_popup, child_disabled) or produced
            if own is not None and own.replaced:
                # a focusable descendant stands for this control
                return True
        if not relevant:
            return produced
        if produced and suppress_container(role, parent_role, lambda: node.name):
            return True
        if rect.empty or not _inside(rect, clips):
            return produced
        name = node.name
        if target_ancestor is not None:
            if role == target_ancestor.role and (not name or _same_rect(rect, target_ancestor.rect)):
                # One control, two targets — keep the outer one. Test desktop
                # 2026-11-11: System Settings theme tiles (unnamed radio button
                # inside radio button 'Breeze'); Plasma panel (button inside
                # button with the same rect: launcher, tray icons, clock).
                return produced
            if name and name == target_ancestor.name and _contains(target_ancestor.rect, rect):
                # The same, with different roles — keep the focusable one,
                # else the outer. Test desktop 2026-11-12, gtk_testapp: a
                # GTK4 MenuButton is a non-focusable "button" around a
                # focusable "toggle button"; a tree row is a "list item"
                # around its TreeExpander's focusable "button" (4 px
                # smaller), all with the same name.
                if FOCUSABLE in states and not target_ancestor.focusable:
                    target_ancestor.replaced = True
                else:
                    return produced
        out.append(RawElement(node.id, role, name, rect, clips, node.ref,
                              web=web_from is not None,
                              web_clips=0 if web_from is None else len(clips) - web_from,
                              popup=popup))
        return True

    def walk_menu(menu, clips, depth):
        """Children of a popup menu, with menu_repair for items Qt reports
        as not showing with another menu's geometry."""
        children = []
        for child in menu.children:
            rect = child.rect
            showing = SHOWING in child.states
            if showing or not rect.empty:
                children.append((child, rect, showing))
        menu_rect = menu.rect
        repaired = repair_menu_layout([(r, ok) for _, r, ok in children], menu_rect)
        if os.environ.get("DECKERY_DEBUG_MENUS"):
            print(f"[menu] {menu.name!r} {menu_rect} depth={depth}", file=sys.stderr)
            for index, (child, rect, showing) in enumerate(children):
                print(f"[menu]   {child.role} {child.name!r} {rect} "
                      f"showing={showing} repaired={repaired.get(index)}", file=sys.stderr)
        produced = False
        for index, (child, rect, showing) in enumerate(children):
            if index in repaired and is_menu_item_candidate(child.role, child.states, child.alive):
                stats.nodes += 1
                out.append(RawElement(child.id, child.role, child.name, repaired[index], clips, child.ref,
                                      inferred=True))
                produced = True
            else:
                produced = walk(child, clips, depth + 1) or produced
        return produced

    walk(root, (), 0)
    # Qt reports a submenu's WINDOW coordinates relative to its parent menu
    # (Dolphin's "Create New" opening leftwards: x=-316), while it is placed
    # on screen as its own popup surface: rebase onto the popup's own origin.
    # rebase: the same for a popup living inside its main window's tree
    # (Firefox, backend._match_embedded_popups).
    if rebase or root.role == "popup menu":
        base = root.rect
        if base.x or base.y:
            out = [_rebase(e, base.x, base.y) for e in out]
    return out


@dataclass
class _Ancestor:
    role: str
    rect: Rect
    name: str
    focusable: bool
    replaced: bool = False   # a focusable same-named descendant is the target instead


def _rebase(element, dx, dy):
    def shift(r):
        return Rect(r.x - dx, r.y - dy, r.w, r.h)
    return replace(element, rect=shift(element.rect), clips=tuple(shift(c) for c in element.clips))


# ---- source 1: libatspi, one call per property ----

class LiveNode:
    def __init__(self, acc):
        self.ref = acc

    @functools.cached_property
    def id(self):
        return element_id(self.ref)

    @functools.cached_property
    def role(self):
        return AXObject.get_role_name(self.ref) or ""

    @functools.cached_property
    def states(self):
        return states_from_state_set(AXObject.get_state_set(self.ref))

    @functools.cached_property
    def rect(self):
        return _rect_of(self.ref)

    @functools.cached_property
    def name(self):
        return AXObject.get_name(self.ref) or ""

    @functools.cached_property
    def children(self):
        return [LiveNode(c) for c in AXObject.iter_children(self.ref)]

    @functools.cached_property
    def alive(self):
        return not AXObject.is_dead(self.ref)


# ---- source 2: pipelined D-Bus fetch ----

def _role_names():
    """GetRole code → libatspi's role name (atspi_accessible_get_role_name:
    the enum nick with spaces; EXTENDED and unknown codes go through
    GetRoleName instead)."""
    names = {}
    for code, role in Atspi.Role.__enum_values__.items():
        if role not in (Atspi.Role.EXTENDED, Atspi.Role.LAST_DEFINED):
            names[code] = Atspi.role_get_name(role)
    return names


_ROLE_NAMES = _role_names()
_WINDOW_COORDS = GLib.Variant("(u)", (COORD_TYPE_WINDOW,))
_NAME_PROPERTY = GLib.Variant("(ss)", (ACCESSIBLE, "Name"))
_CHILD_COUNT_PROPERTY = GLib.Variant("(ss)", (ACCESSIBLE, "ChildCount"))


class AccessibleRef:
    """Finds a fetched object's Atspi.Accessible on demand: by child
    indices from the frame (one libatspi call per level), checked against
    the element id in case the tree changed since the read."""

    def __init__(self, root, index_path, eid):
        self._root = root
        self._index_path = index_path
        self._id = eid
        self._resolved = None

    def resolve(self):
        if self._resolved is None:
            acc = self._root
            try:
                for index in self._index_path:
                    acc = acc.get_child_at_index(index)
                    if acc is None:
                        return None
            except GLib.Error:
                return None
            if element_id(acc) != self._id:
                return None
            self._resolved = acc
        return self._resolved


class FetchedNode:
    __slots__ = ("dest", "path", "index_path", "role", "states", "child_refs", "alive",
                 "_children", "_rect", "_name", "_tree")

    def __init__(self, tree, dest, path, index_path):
        self._tree = tree
        self.dest = dest
        self.path = path
        self.index_path = index_path
        self.role = ""
        self.states = frozenset()
        self.child_refs = ()
        self._children = None
        self.alive = True
        self._rect = None
        self._name = None

    @property
    def id(self):
        return self.dest + self.path

    @property
    def ref(self):
        return AccessibleRef(self._tree.root_acc, self.index_path, self.id)

    @property
    def rect(self):
        if self._rect is None:
            self._tree.stats.fallbacks += 1
            reply = self._tree.bus.call_sync(self.dest, self.path, COMPONENT, "GetExtents", _WINDOW_COORDS)
            self._rect = Rect(*reply[0]) if reply else Rect(0, 0, 0, 0)
        return self._rect

    @property
    def children(self):
        if self._children is None:
            # Can't be fetched on demand like rect/name (it's a subtree);
            # would mean _descend and the walk disagree.
            self._tree.stats.fallbacks += 1
            return []
        return self._children

    @property
    def name(self):
        if self._name is None:
            self._tree.stats.fallbacks += 1
            reply = self._tree.bus.call_sync(self.dest, self.path, PROPERTIES, "Get", _NAME_PROPERTY)
            self._name = reply[0] if reply else ""
        return self._name


class _TreeFetch:
    """Fetches exactly what select_targets will read. Per node, role,
    states and the child list are asked at once (the child list
    speculatively: one reply wasted on leaves and pruned nodes, but no
    extra round trip per tree level). Once role and states are in:
    extents only for clipping containers and possible targets, the name
    only for possible targets; children are descended into as soon as the
    shared top-down rules allow."""

    def __init__(self, bus, batch, root_acc, prune_hidden, stats, inherit_disabled=False):
        self.bus = bus
        self.root_acc = root_acc
        self.stats = stats
        self._batch = batch
        self._prune_hidden = prune_hidden
        self._inherit_disabled = inherit_disabled

    def start(self):
        acc = self.root_acc
        root = FetchedNode(self, acc.app.bus_name if acc.app else "", acc.path, ())
        self._visit(root, (), 0, in_menu=False, parent_role="", probe=False, disabled=False)
        return root

    def _call(self, node, iface, method, args, on_reply):
        self._batch.call(node.dest, node.path, iface, method, args, on_reply)

    def _visit(self, node, clips, depth, in_menu, parent_role, probe, disabled):
        waiting = [3]

        def step():
            waiting[0] -= 1
            if not waiting[0]:
                self._decide(node, clips, depth, in_menu, parent_role, probe, disabled)

        def on_role(reply):
            if reply is None:
                node.alive = False
            elif reply[0] in _ROLE_NAMES:
                node.role = _ROLE_NAMES[reply[0]]
            else:
                waiting[0] += 1
                self._call(node, ACCESSIBLE, "GetRoleName", None, on_role_name)
            step()

        def on_role_name(reply):
            node.role = reply[0] if reply else ""
            step()

        def on_states(reply):
            if reply is not None:
                words = list(reply[0]) + [0, 0]
                node.states = states_from_bits(words[0], words[1])
            step()

        def on_children(refs):
            node.child_refs = refs
            step()

        self._call(node, ACCESSIBLE, "GetRole", None, on_role)
        self._call(node, ACCESSIBLE, "GetState", None, on_states)
        self._fetch_children(node, on_children)

    def _fetch_children(self, node, done):
        """[(dest, path) or None per child index]. GetChildren where the
        toolkit has it, else ChildCount + GetChildAtIndex like libatspi."""
        def on_children(reply):
            if reply is not None:
                done([(d, p) if p != NULL_PATH else None for d, p in reply[0]])
            else:
                self._call(node, PROPERTIES, "Get", _CHILD_COUNT_PROPERTY, on_count)

        def on_count(reply):
            count = reply[0] if reply else 0
            if count <= 0:
                done([])
                return
            refs = [None] * count
            waiting = [count]

            def on_child(index, reply):
                if reply is not None and reply[0][1] != NULL_PATH:
                    refs[index] = tuple(reply[0])
                waiting[0] -= 1
                if not waiting[0]:
                    done(refs)

            for i in range(count):
                self._call(node, ACCESSIBLE, "GetChildAtIndex", GLib.Variant("(i)", (i,)),
                           functools.partial(on_child, i))

        self._call(node, ACCESSIBLE, "GetChildren", None, on_children)

    def _decide(self, node, clips, depth, in_menu, parent_role, probe, disabled):
        role = node.role
        # Not walked: nothing but a scroll probe's rect is looked at.
        walked = not _closed_combo_popup(role, parent_role)
        asked_name = []
        relevant = walked and depth > 0 and not disabled and \
            is_relevant(role, node.states, lambda: asked_name.append(True) or "")
        # Menu children: walk_menu looks at every child's rect, and names
        # the ones menu_repair reconstructs. The frame itself: rebasing
        # (popup menus) and the menu debug log.
        want_name = relevant or asked_name or in_menu or depth == 0
        want_rect = want_name or probe or (walked and (role in CLIPPING_ROLES
                                                       or _children_rect_needed(role, node.states)))
        descend_after_rect = walked and role in CLIPPING_ROLES
        if want_name:
            def on_name(reply):
                node._name = reply[0] if reply else ""
                node.alive = reply is not None
            self._call(node, PROPERTIES, "Get", _NAME_PROPERTY, on_name)
        if want_rect:
            def on_rect(reply):
                node._rect = Rect(*reply[0]) if reply else Rect(0, 0, 0, 0)
                if descend_after_rect:
                    self._descend(node, clips, depth, disabled)
            self._call(node, COMPONENT, "GetExtents", _WINDOW_COORDS, on_rect)
        if walked and not descend_after_rect:
            self._descend(node, clips, depth, disabled)

    def _descend(self, node, clips, depth, disabled=False):
        child_clips = clips
        if node.role in CLIPPING_ROLES:
            child_clips = _child_clips(node.role, node._rect, clips)
            if child_clips is None:
                return
        descent = _descent(node.role, node.states, depth, self._prune_hidden)
        if descent is LEAF or descent is SUBMENU:
            return
        # As in the walk: walk_menu passes no parent role, and only plain
        # children of a clipping container have a scroll probe.
        in_menu = descent is MENU
        as_parent = "" if in_menu else _role_for_children(node.role, node.states)
        probe = not in_menu and child_clips is not clips
        placed = not in_menu and _children_rect_needed(node.role, node.states)
        if not in_menu:
            disabled = disabled or (self._inherit_disabled and depth > 0 and not is_enabled(node.states))
        node._children = []
        for index, ref in enumerate(node.child_refs):
            if ref is None:
                continue  # libatspi skips null references too
            child = FetchedNode(self, ref[0], ref[1], node.index_path + (index,))
            node._children.append(child)
            self._visit(child, child_clips, depth + 1, in_menu, as_parent, probe or placed,
                        disabled and not in_menu)
            probe = False


def fetch_tree(bus, frame, prune_hidden, stats, inherit_disabled=False):
    with bus.batch() as batch:
        fetch = _TreeFetch(bus, batch, frame, prune_hidden, stats, inherit_disabled)
        root = fetch.start()
        batch.run()
    stats.calls = batch.calls
    return root


# ---- entry points ----

def read_window(frame, toolkit="", mode=None, rebase=False):
    mode = mode or READER_MODE
    if mode == "compare":
        return _read_compare(frame, toolkit, rebase)
    stats = ReadStats()
    prune_hidden = (toolkit or "").lower() in PRUNE_NOT_SHOWING_TOOLKITS
    inherit_disabled = (toolkit or "").lower() in INHERIT_DISABLED_TOOLKITS
    t0 = time.monotonic()
    bus = shared_bus() if mode != "sync" else None
    root = fetch_tree(bus, frame, prune_hidden, stats, inherit_disabled) if bus else LiveNode(frame)
    out = select_targets(root, prune_hidden, stats, rebase, inherit_disabled)
    stats.ms = 1000 * (time.monotonic() - t0)
    return out, stats


_compare_sync_first = False


def _read_compare(frame, toolkit, rebase):
    """Both readers on the same frame, alternating which goes first (the
    first read of a tree warms the app's side). Returns the fetched result;
    differences go to stderr."""
    global _compare_sync_first
    _compare_sync_first = not _compare_sync_first
    if _compare_sync_first:
        old, old_stats = read_window(frame, toolkit, "sync", rebase)
        new, stats = read_window(frame, toolkit, "async", rebase)
    else:
        new, stats = read_window(frame, toolkit, "async", rebase)
        old, old_stats = read_window(frame, toolkit, "sync", rebase)
    stats.note = f"/sync={old_stats.ms:.0f}ms/{stats.calls}c"
    if new != old:
        stats.note += "/DIFF"
        print(f"[compare] {element_id(frame)}: sync {len(old)} vs fetched {len(new)} elements, "
              f"nodes {old_stats.nodes}/{stats.nodes}", file=sys.stderr)
        new_set, old_set = set(new), set(old)
        for e in old:
            if e not in new_set:
                print(f"[compare]   only sync:    {e}", file=sys.stderr)
        for e in new:
            if e not in old_set:
                print(f"[compare]   only fetched: {e}", file=sys.stderr)
        if new_set == old_set:
            print("[compare]   same elements, different order", file=sys.stderr)
    return new, stats


def _split_id(eid):
    slash = eid.find("/")
    return (eid[:slash], eid[slash:]) if slash > 0 else (None, None)


def live_rects(elements):
    """Current window-relative rects of elements, asked all at once (the
    active-window freshness poll). Rect(0, 0, 0, 0) where the object is
    gone or has no extents."""
    bus = shared_bus() if READER_MODE != "sync" else None
    if bus is None:
        return [live_rect(e.accessible) for e in elements]
    rects = [Rect(0, 0, 0, 0)] * len(elements)

    def store(index, reply):
        if reply is not None:
            rects[index] = Rect(*reply[0])

    with bus.batch() as batch:
        for index, element in enumerate(elements):
            dest, path = _split_id(element.id)
            if dest:
                batch.call(dest, path, COMPONENT, "GetExtents", _WINDOW_COORDS,
                           functools.partial(store, index))
        batch.run()
    return rects
