"""An application that stops answering AT-SPI must not freeze the backend.

Real desktop 2026-11-12, under heavy load: plasmashell missed the frame
list's ping, its previous frames were kept (so the panels stayed matched),
and every update still re-read all of its windows — each read waited for
three 3 s timeouts, one update took 45 s, three in a row: the backend
stood still for 2.5 min and the panel outlines stayed where they were."""

import unittest
from types import SimpleNamespace

from gi.repository import Gio, GLib

from deckery_atspi import backend as backend_module
from deckery_atspi.atspi_bus import Batch
from deckery_atspi.atspi_frames import FrameInfo
from deckery_atspi.geometry import Rect
from deckery_atspi.reader import RawElement, ReadStats
from deckery_atspi.scene import scene_from_payload

TIMEOUT = GLib.Error.new_literal(Gio.io_error_quark(), "Timeout was reached", Gio.IOErrorEnum.TIMED_OUT)


class FakeConnection:
    """Answers every call at once; paths in `hung` time out."""

    def __init__(self, hung=()):
        self.hung = set(hung)
        self.sent = []

    def call(self, dest, path, iface, method, args, reply_type, flags, timeout_ms, cancellable,
             callback, user_data):
        self.sent.append((path, method))
        callback(self, path, user_data)

    def call_finish(self, path):
        if path in self.hung:
            raise TIMEOUT
        return GLib.Variant("(i)", (1,))


class BatchTimeoutTest(unittest.TestCase):
    def test_no_more_calls_after_a_timeout(self):
        connection = FakeConnection(hung={"/a"})
        batch = Batch(GLib.MainContext.new(), connection)
        replies = []
        batch.call(":1.10", "/a", "i", "GetRole", None, replies.append)
        self.assertTrue(batch.timed_out)
        # The app is hung: a further call would only wait out its own
        # timeout. It is answered with None right away instead.
        batch.call(":1.10", "/b", "i", "GetRole", None, replies.append)
        batch.run()
        self.assertEqual(connection.sent, [("/a", "GetRole")])
        self.assertEqual(replies, [None, None])

    def test_other_errors_keep_going(self):
        connection = FakeConnection()
        batch = Batch(GLib.MainContext.new(), connection)
        replies = []
        batch.call(":1.10", "/a", "i", "GetRole", None, replies.append)
        batch.call(":1.10", "/b", "i", "GetRole", None, replies.append)
        self.assertFalse(batch.timed_out)
        self.assertEqual(len(connection.sent), 2)


PANEL = ":1.10"


def panel_scene(hidden=False, active=""):
    window = {"id": "{panel}", "pid": 100, "caption": "", "resourceClass": "plasmashell", "layer": 3,
              "output": "eDP-1", "frame": {"x": 0, "y": 142, "w": 66, "h": 442},
              "desktops": [], "onAllDesktops": True, "hidden": hidden, "flags": ["dock"]}
    return scene_from_payload({"version": 2, "currentDesktop": "d1", "activeWindow": active,
                               "outputs": [{"name": "eDP-1", "geometry": {"x": 0, "y": 0, "w": 1164, "h": 727},
                                            "scale": 1.1}],
                               "windows": [window]})


class HungAppTest(unittest.TestCase):
    def setUp(self):
        self.reads = []
        self.hung = False
        self.probes = []
        original = backend_module.read_window

        def fake_read(accessible, toolkit, rebase=False):
            self.reads.append(accessible)
            stats = ReadStats(nodes=1 if self.hung else 3)
            if self.hung:
                stats.timed_out = True
                return [], stats
            return [RawElement(PANEL + "/launcher", "button", "Application Launcher",
                               Rect(12, 16, 42, 42), ())], stats

        backend_module.read_window = fake_read
        self.addCleanup(setattr, backend_module, "read_window", original)
        self.backend = backend_module.Backend(log=lambda msg: None)
        self.backend._probe_app = self.probes.append
        self.backend.frames = [FrameInfo(id=PANEL + "/panel", app_name="plasmashell", pid=100, toolkit="Qt",
                                         toolkit_version="6", role="frame", name="", width=66, height=442,
                                         showing=True, active=False, accessible="panel-frame")]
        self.backend._frames_listed_at = 1e12  # no re-list in these tests

    def names(self):
        return [e.name for e in self.backend.elements]

    def test_timed_out_read_marks_app_and_keeps_targets(self):
        self.backend.on_scene(panel_scene())
        self.assertEqual(self.names(), ["Application Launcher"])
        self.hung = True
        self.backend._update(reread_bus_names={PANEL})
        self.assertIn(PANEL, self.backend._unresponsive_apps)
        self.assertEqual(self.probes, [PANEL])
        # Its last known targets stay: better than none while it hangs.
        self.assertEqual(self.names(), ["Application Launcher"])

    def test_hung_app_is_not_read_again(self):
        self.backend.on_scene(panel_scene())
        self.backend._mark_unresponsive(PANEL)
        self.reads.clear()
        for _ in range(3):
            self.backend._update(reread_bus_names={PANEL})
        self.assertEqual(self.reads, [])
        self.assertEqual(self.names(), ["Application Launcher"])

    def test_kwin_still_hides_a_hung_apps_panel(self):
        # The symptom: an auto-hide panel slid out, its outlines stayed.
        self.backend.on_scene(panel_scene())
        self.backend._mark_unresponsive(PANEL)
        self.backend.on_scene(panel_scene(hidden=True))
        self.assertEqual(self.names(), [])
        self.backend.on_scene(panel_scene(hidden=False))
        self.assertEqual(self.names(), ["Application Launcher"])

    def test_active_window_poll_timing_out_marks_app(self):
        self.backend.on_scene(panel_scene(active="{panel}"))
        original = backend_module.live_rects
        polled = []
        backend_module.live_rects = lambda samples: polled.append(samples) and None
        try:
            self.backend._poll_active_window()
            self.assertIn(PANEL, self.backend._unresponsive_apps)
            self.backend._poll_active_window()  # not asked again while hung
        finally:
            backend_module.live_rects = original
        self.assertEqual(len(polled), 1)

    def test_answering_again_ends_it_at_once(self):
        self.backend.on_scene(panel_scene())
        self.backend._mark_unresponsive(PANEL)
        self.backend._on_probe_reply(PANEL, answered=True)
        self.assertNotIn(PANEL, self.backend._unresponsive_apps)
        self.assertIsNone(self.backend.frames)  # re-listed on the next update
        self.assertIn(PANEL, self.backend._dirty_bus_names)

    def test_still_hung_is_probed_again(self):
        self.backend._mark_unresponsive(PANEL)
        self.probes.clear()
        retries = []
        original = GLib.timeout_add
        GLib.timeout_add = lambda ms, fn: retries.append(fn) or 1
        try:
            self.backend._on_probe_reply(PANEL, answered=False)
        finally:
            GLib.timeout_add = original
        self.assertIn(PANEL, self.backend._unresponsive_apps)
        retries[0]()
        self.assertEqual(self.probes, [PANEL])


if __name__ == "__main__":
    unittest.main()
