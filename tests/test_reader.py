"""The reader's walk (select_targets) on hand-built trees, and the batched
fetch (fetch_tree) against a fake AT-SPI bus: it must hand the walk the
same answers a full read would, without fallbacks and without asking for
what the walk never looks at."""

import unittest
from collections import deque
from types import SimpleNamespace

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from deckery_atspi import eligibility as el
from deckery_atspi.geometry import Rect
from deckery_atspi.reader import ReadStats, fetch_tree, select_targets

S = Atspi.StateType
TARGET = (S.SHOWING, S.ENABLED, S.FOCUSABLE)
SHOWN = (S.SHOWING, S.ENABLED)


class Node:
    """Fully known node (what LiveNode would return); also the fake bus'
    data source."""

    def __init__(self, path, role, states=(), rect=(0, 0, 0, 0), name="", children=()):
        self.path = path
        self.id = ":1.9" + path
        self.role_code = role
        self.role = Atspi.role_get_name(role)
        self.states = frozenset(int(s) for s in states)
        self.rect = Rect(*rect)
        self.name = name
        self.children = list(children)
        self.alive = True
        self.ref = None


def ids(elements):
    return [e.id.rsplit("/", 1)[1] for e in elements]


def walk(root, prune_hidden=False, stats=None):
    return select_targets(root, prune_hidden, stats if stats is not None else ReadStats())


class EligibilityTest(unittest.TestCase):
    def test_states_from_bits(self):
        # GetState: bit n of the two words (low first) = state n
        self.assertEqual(el.states_from_bits(0b101, 0b10), frozenset((0, 2, 33)))
        self.assertIn(int(S.SHOWING), el.states_from_bits(1 << int(S.SHOWING), 0))

    def test_gtk4_sensitive_counts_as_enabled(self):
        states = frozenset(int(s) for s in (S.SHOWING, S.SENSITIVE, S.FOCUSABLE))
        self.assertTrue(el.is_relevant("button", states, lambda: ""))

    def test_focused_counts_as_focusable(self):
        states = frozenset(int(s) for s in (S.SHOWING, S.ENABLED, S.FOCUSED))
        self.assertTrue(el.is_relevant("button", states, lambda: ""))

    def test_name_only_asked_where_it_matters(self):
        asked = []
        states = frozenset(int(s) for s in TARGET)
        el.is_relevant("button", states, lambda: asked.append(1) or "")
        self.assertEqual(asked, [])
        self.assertTrue(el.is_relevant("menu", frozenset((int(S.ENABLED),)), lambda: "File"))
        self.assertTrue(el.is_relevant("canvas", frozenset(int(s) for s in SHOWN), lambda: "Home"))
        self.assertFalse(el.is_relevant("canvas", frozenset(int(s) for s in SHOWN), lambda: ""))
        self.assertFalse(el.is_relevant("label", states, lambda: "x"))


class WalkTest(unittest.TestCase):
    def test_container_suppressed_by_eligible_child(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.PANEL, TARGET, (0, 0, 100, 100), children=[
                Node("/2", Atspi.Role.PUSH_BUTTON, TARGET, (10, 10, 20, 20), "OK")])])
        self.assertEqual(ids(walk(root)), ["2"])

    def test_nested_duplicate_keeps_outer(self):
        # Plasma panel: button inside button with the same rect.
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.PUSH_BUTTON, TARGET, (0, 0, 40, 40), "Clock", children=[
                Node("/2", Atspi.Role.PUSH_BUTTON, TARGET, (0, 0, 40, 40), "Clock")])])
        self.assertEqual(ids(walk(root)), ["1"])

    def test_clipped_away_subtree_not_walked(self):
        far = Node("/3", Atspi.Role.LIST, SHOWN, (0, 500, 100, 100), children=[
            Node("/4", Atspi.Role.LIST_ITEM, SHOWN, (0, 500, 100, 20), "far")])
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.SCROLL_PANE, SHOWN, (0, 0, 100, 100), children=[
                Node("/2", Atspi.Role.LIST_ITEM, SHOWN, (0, 0, 100, 20), "near"), far])])
        stats = ReadStats()
        out = walk(root, False, stats)
        self.assertEqual(ids(out), ["2"])
        self.assertEqual(out[0].clips, (Rect(0, 0, 100, 100),))
        self.assertEqual(stats.pruned, 1)
        # scroll probe: first child of the clipping container
        self.assertEqual([(p.id[-2:], p.rect) for p in stats.probes], [("/2", Rect(0, 0, 100, 20))])

    def test_qt_hidden_subtree_pruned(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.MENU, (S.ENABLED,), children=[
                Node("/2", Atspi.Role.MENU_ITEM, SHOWN, (0, 0, 10, 10), "Quit")])])
        self.assertEqual(ids(walk(root, prune_hidden=True)), [])
        self.assertEqual(ids(walk(root, prune_hidden=False)), ["2"])

    def test_popup_menu_repair_and_rebase(self):
        # Root popup menu at (100, 50): repaired items inferred, all rebased
        # onto the popup; a nested popup menu (submenu) is not entered.
        menu = Node("/0", Atspi.Role.POPUP_MENU, SHOWN, (100, 50, 200, 100), "ctx", children=[
            Node("/1", Atspi.Role.MENU_ITEM, SHOWN, (104, 54, 192, 30), "Open"),
            Node("/2", Atspi.Role.MENU_ITEM, (S.ENABLED,), (4, 900, 150, 30), "Cut"),
            Node("/3", Atspi.Role.MENU_ITEM, SHOWN, (104, 114, 192, 30), "Delete"),
            Node("/4", Atspi.Role.POPUP_MENU, SHOWN, (300, 50, 100, 100), "sub", children=[
                Node("/5", Atspi.Role.MENU_ITEM, SHOWN, (300, 50, 100, 30), "inner")])])
        out = walk(menu)
        self.assertEqual(ids(out), ["1", "2", "3"])
        self.assertEqual([e.inferred for e in out], [False, True, False])
        self.assertEqual(out[1].rect, Rect(4, 34, 192, 30))
        self.assertEqual(out[0].rect, Rect(4, 4, 192, 30))

    def test_web_space_and_clips(self):
        # Chromium: the browser UI is not web space; "document web" and
        # everything in it is, and so are the clips from it inwards.
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.SCROLL_PANE, SHOWN, (0, 0, 500, 500), children=[
                Node("/2", Atspi.Role.PUSH_BUTTON, TARGET, (0, 0, 20, 20), "Back"),
                Node("/3", Atspi.Role.DOCUMENT_WEB, TARGET, (0, 50, 400, 400), "Page", children=[
                    Node("/4", Atspi.Role.LIST, SHOWN, (0, 50, 300, 300), children=[
                        Node("/5", Atspi.Role.LINK, TARGET, (5, 60, 50, 10), "Link")])])])])
        out = walk(root)
        self.assertEqual(ids(out), ["2", "5"])
        # clips of the link: scroll pane | document, list (the document
        # clips too, and is web space itself)
        self.assertEqual([(e.web, e.web_clips) for e in out], [(False, 0), (True, 2)])
        self.assertEqual(len(out[1].clips), 3)

    def test_document_alone_is_a_target(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.DOCUMENT_WEB, TARGET, (0, 0, 400, 400), "Empty page")])
        out = walk(root)
        self.assertEqual(ids(out), ["1"])
        self.assertEqual((out[0].web, out[0].web_clips), (True, 0))

    def test_closed_combo_box_options_skipped(self):
        def combo(path, states):
            return Node(path, Atspi.Role.COMBO_BOX, states, (0, 0, 100, 20), "Pick", children=[
                Node(path + "1", Atspi.Role.MENU, (S.ENABLED,), (0, 20, 100, 60), children=[
                    Node(path + "2", Atspi.Role.MENU_ITEM, TARGET, (0, 20, 100, 20), "b")])])
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            combo("/1", TARGET), combo("/5", TARGET + (S.EXPANDED,))])
        # expanded: the option is a target and suppresses nothing (combo
        # box isn't a container role); closed: only the combo box itself
        self.assertEqual(ids(walk(root)), ["1", "52", "5"])

    def test_tab_in_tab_list_stays_target(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.PAGE_TAB_LIST, SHOWN, (0, 0, 400, 30), children=[
                Node("/2", Atspi.Role.PAGE_TAB, TARGET, (0, 0, 100, 30), "Tab", children=[
                    Node("/3", Atspi.Role.PUSH_BUTTON, TARGET, (80, 5, 15, 15), "Close")])]),
            Node("/4", Atspi.Role.FILLER, SHOWN, (0, 30, 400, 300), children=[
                Node("/5", Atspi.Role.PAGE_TAB, TARGET, (0, 30, 400, 300), "Page", children=[
                    Node("/6", Atspi.Role.PUSH_BUTTON, TARGET, (10, 40, 20, 20), "OK")])])])
        self.assertEqual(ids(walk(root)), ["3", "2", "6"])


# ---- fetch_tree against a fake bus ----

class FakeBatch:
    def __init__(self, bus):
        self.bus = bus
        self.calls = 0
        self.queue = deque()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, dest, path, iface, method, args, on_reply):
        self.calls += 1
        self.bus.log.append((path, method if method != "Get" else args.unpack()[1]))
        self.queue.append((on_reply, self.bus.reply(path, iface, method, args)))

    def run(self):
        # Replies in an order unlike the requests, to shake out ordering
        # assumptions: newest first.
        while self.queue:
            on_reply, value = self.queue.pop()
            on_reply(value)


class FakeBus:
    def __init__(self, root, broken=()):
        self.nodes = {}
        self.log = []
        self.broken = set(broken)
        self._index(root)

    def _index(self, node):
        self.nodes[node.path] = node
        for child in node.children:
            self._index(child)

    def batch(self):
        return FakeBatch(self)

    def reply(self, path, iface, method, args):
        node = self.nodes.get(path)
        if node is None or path in self.broken:
            return None
        if method == "GetRole":
            return (int(node.role_code),)
        if method == "GetState":
            bits = sum(1 << s for s in node.states)
            return ([bits & 0xFFFFFFFF, bits >> 32],)
        if method == "GetChildren":
            return ([(":1.9", c.path) for c in node.children],)
        if method == "GetExtents":
            r = node.rect
            return ((r.x, r.y, r.w, r.h),)
        if method == "Get":
            return (node.name,)
        raise AssertionError(method)

    def call_sync(self, dest, path, iface, method, args=None):
        raise AssertionError(f"fallback call {method} on {path}")


def fetched_walk(root, prune_hidden=False):
    bus = FakeBus(root)
    stats = ReadStats()
    frame = SimpleNamespace(app=SimpleNamespace(bus_name=":1.9"), path=root.path)
    tree = fetch_tree(bus, frame, prune_hidden, stats)
    return select_targets(tree, prune_hidden, stats), stats, bus


class FetchTest(unittest.TestCase):
    TREES = [
        WalkTest.test_container_suppressed_by_eligible_child,
        WalkTest.test_nested_duplicate_keeps_outer,
        WalkTest.test_clipped_away_subtree_not_walked,
        WalkTest.test_qt_hidden_subtree_pruned,
        WalkTest.test_popup_menu_repair_and_rebase,
        WalkTest.test_web_space_and_clips,
        WalkTest.test_document_alone_is_a_target,
        WalkTest.test_closed_combo_box_options_skipped,
        WalkTest.test_tab_in_tab_list_stays_target,
    ]

    def test_same_as_full_read(self):
        # Rerun every walk test, with each walk going through fetch_tree.
        import tests.test_reader as module
        original = module.walk
        try:
            def via_fetch(root, prune_hidden=False, stats=None):
                out, fetched_stats, _ = fetched_walk(root, prune_hidden)
                self.assertEqual(fetched_stats.fallbacks, 0)
                full_stats = ReadStats()
                self.assertEqual(out, original(root, prune_hidden, full_stats))
                self.assertEqual(fetched_stats.probes, full_stats.probes)
                self.assertEqual((fetched_stats.nodes, fetched_stats.pruned),
                                 (full_stats.nodes, full_stats.pruned))
                if stats is not None:
                    stats.__dict__.update(fetched_stats.__dict__)
                return out
            module.walk = via_fetch
            for test in self.TREES:
                test(self)
        finally:
            module.walk = original

    def test_only_what_the_walk_needs(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.FILLER, SHOWN, (0, 0, 100, 100), "big", children=[
                Node("/2", Atspi.Role.LABEL, SHOWN, (0, 0, 10, 10), "text"),
                Node("/3", Atspi.Role.PUSH_BUTTON, TARGET, (10, 10, 20, 20), "OK")])])
        out, stats, bus = fetched_walk(root)
        self.assertEqual(ids(out), ["3"])
        asked = {(p, m) for p, m in bus.log}
        # neither the filler nor the label is a possible target
        for path in ("/1", "/2"):
            self.assertNotIn((path, "GetExtents"), asked)
            self.assertNotIn((path, "Name"), asked)
        self.assertIn(("/3", "GetExtents"), asked)
        self.assertIn(("/3", "Name"), asked)

    def test_dead_node_is_empty(self):
        root = Node("/0", Atspi.Role.FRAME, SHOWN, children=[
            Node("/1", Atspi.Role.PUSH_BUTTON, TARGET, (0, 0, 20, 20), "gone"),
            Node("/2", Atspi.Role.PUSH_BUTTON, TARGET, (30, 0, 20, 20), "OK")])
        bus = FakeBus(root, broken={"/1"})
        stats = ReadStats()
        frame = SimpleNamespace(app=SimpleNamespace(bus_name=":1.9"), path="/0")
        out = select_targets(fetch_tree(bus, frame, False, stats), False, stats)
        self.assertEqual(ids(out), ["2"])


if __name__ == "__main__":
    unittest.main()
