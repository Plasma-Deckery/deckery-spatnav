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
from .eligibility import (EXPANDED, SHOWING, is_menu_item_candidate, is_relevant, states_from_bits,
                          states_from_state_set, suppress_container)
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


# ---- top-down rules, shared by the walk and fetch_tree ----
#
# fetch_tree has to decide which children to fetch before the walk runs;
# both go through these two functions, so they can't disagree.

LEAF, SUBMENU, MENU, CHILDREN = "leaf", "submenu", "menu", "children"


def _child_clips(rect, clips):
    """For a clipping container: the clips its children are cut to, or
    None if it lies entirely outside its own clipping ancestors (whole
    subtree skipped). An empty rect clips nothing."""
    if rect.empty:
        return clips
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


def _descent(role, showing, depth, prune_hidden):
    if depth >= MAX_DEPTH or (prune_hidden and not showing and depth > 0):
        return LEAF
    if role == "popup menu":
        # A submenu is its own surface (own KWin popup window), read as
        # its own frame via popup tracking — not part of this one.
        return SUBMENU if depth > 0 else MENU
    return CHILDREN


# ---- the walk ----

def select_targets(root, prune_hidden, stats):
    """The targets under root. Nodes provide id, role, states (set of
    Atspi.StateType ints), rect, name, children, alive and ref; the walk
    reads each only where it needs it (a LiveNode turns every first read
    into one libatspi call)."""
    out = []

    def walk(node, clips, depth, target_ancestor=None, parent_role="", web_from=None):
        """Returns True if node or a descendant produced an element.
        target_ancestor: (role, rect) of the nearest ancestor that is itself
        a target (eligibility is known pre-order from role + states).
        web_from: number of clips outside the web document this node is in."""
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
            child_clips = _child_clips(rect, clips)
            if child_clips is None:
                stats.pruned += 1
                return False
        relevant = is_relevant(role, states, lambda: node.name)
        if relevant and rect is None:
            rect = node.rect
        produced = False
        descent = _descent(role, SHOWING in states, depth, prune_hidden)
        if descent is LEAF:
            stats.pruned += depth < MAX_DEPTH
        elif descent is SUBMENU:
            return False
        elif descent is MENU:
            produced = walk_menu(node, child_clips, depth)
        else:
            child_ancestor = (role, rect) if relevant else target_ancestor
            as_parent = _role_for_children(role, states)
            probe = child_clips is not clips
            for child in node.children:
                if probe:
                    stats.probes.append(Probe(child.id, child.rect, child.ref))
                    probe = False
                produced = walk(child, child_clips, depth + 1, child_ancestor,
                                as_parent, web_from) or produced
        if not relevant:
            return produced
        if produced and suppress_container(role, parent_role):
            return True
        if rect.empty or not _inside(rect, clips):
            return produced
        name = node.name
        if target_ancestor is not None and role == target_ancestor[0] and \
                (not name or _same_rect(rect, target_ancestor[1])):
            # One control, two targets — keep the outer one. Test desktop
            # 2026-11-11: System Settings theme tiles (unnamed radio button
            # inside radio button 'Breeze'); Plasma panel (button inside
            # button with the same rect: launcher, tray icons, clock).
            return produced
        out.append(RawElement(node.id, role, name, rect, clips, node.ref,
                              web=web_from is not None,
                              web_clips=0 if web_from is None else len(clips) - web_from))
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
    if root.role == "popup menu":
        base = root.rect
        if base.x or base.y:
            out = [_rebase(e, base.x, base.y) for e in out]
    return out


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

    def __init__(self, bus, batch, root_acc, prune_hidden, stats):
        self.bus = bus
        self.root_acc = root_acc
        self.stats = stats
        self._batch = batch
        self._prune_hidden = prune_hidden

    def start(self):
        acc = self.root_acc
        root = FetchedNode(self, acc.app.bus_name if acc.app else "", acc.path, ())
        self._visit(root, (), 0, in_menu=False, parent_role="", probe=False)
        return root

    def _call(self, node, iface, method, args, on_reply):
        self._batch.call(node.dest, node.path, iface, method, args, on_reply)

    def _visit(self, node, clips, depth, in_menu, parent_role, probe):
        waiting = [3]

        def step():
            waiting[0] -= 1
            if not waiting[0]:
                self._decide(node, clips, depth, in_menu, parent_role, probe)

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

    def _decide(self, node, clips, depth, in_menu, parent_role, probe):
        role = node.role
        # Not walked: nothing but a scroll probe's rect is looked at.
        walked = not _closed_combo_popup(role, parent_role)
        asked_name = []
        relevant = walked and is_relevant(role, node.states, lambda: asked_name.append(True) or "")
        # Menu children: walk_menu looks at every child's rect, and names
        # the ones menu_repair reconstructs. The frame itself: rebasing
        # (popup menus) and the menu debug log.
        want_name = relevant or asked_name or in_menu or depth == 0
        want_rect = want_name or probe or (walked and role in CLIPPING_ROLES)
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
                    self._descend(node, clips, depth)
            self._call(node, COMPONENT, "GetExtents", _WINDOW_COORDS, on_rect)
        if walked and not descend_after_rect:
            self._descend(node, clips, depth)

    def _descend(self, node, clips, depth):
        child_clips = clips
        if node.role in CLIPPING_ROLES:
            child_clips = _child_clips(node._rect, clips)
            if child_clips is None:
                return
        descent = _descent(node.role, SHOWING in node.states, depth, self._prune_hidden)
        if descent is LEAF or descent is SUBMENU:
            return
        # As in the walk: walk_menu passes no parent role, and only plain
        # children of a clipping container have a scroll probe.
        in_menu = descent is MENU
        as_parent = "" if in_menu else _role_for_children(node.role, node.states)
        probe = not in_menu and child_clips is not clips
        node._children = []
        for index, ref in enumerate(node.child_refs):
            if ref is None:
                continue  # libatspi skips null references too
            child = FetchedNode(self, ref[0], ref[1], node.index_path + (index,))
            node._children.append(child)
            self._visit(child, child_clips, depth + 1, in_menu, as_parent, probe)
            probe = False


def fetch_tree(bus, frame, prune_hidden, stats):
    with bus.batch() as batch:
        fetch = _TreeFetch(bus, batch, frame, prune_hidden, stats)
        root = fetch.start()
        batch.run()
    stats.calls = batch.calls
    return root


# ---- entry points ----

def read_window(frame, toolkit="", mode=None):
    mode = mode or READER_MODE
    if mode == "compare":
        return _read_compare(frame, toolkit)
    stats = ReadStats()
    prune_hidden = (toolkit or "").lower() in PRUNE_NOT_SHOWING_TOOLKITS
    t0 = time.monotonic()
    bus = shared_bus() if mode != "sync" else None
    root = fetch_tree(bus, frame, prune_hidden, stats) if bus else LiveNode(frame)
    out = select_targets(root, prune_hidden, stats)
    stats.ms = 1000 * (time.monotonic() - t0)
    return out, stats


_compare_sync_first = False


def _read_compare(frame, toolkit):
    """Both readers on the same frame, alternating which goes first (the
    first read of a tree warms the app's side). Returns the fetched result;
    differences go to stderr."""
    global _compare_sync_first
    _compare_sync_first = not _compare_sync_first
    if _compare_sync_first:
        old, old_stats = read_window(frame, toolkit, "sync")
        new, stats = read_window(frame, toolkit, "async")
    else:
        new, stats = read_window(frame, toolkit, "async")
        old, old_stats = read_window(frame, toolkit, "sync")
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
