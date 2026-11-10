#!/usr/bin/env python3
"""
deckery_tracker.py — KWin window-tracker D-Bus service + live desktop-
membership state (deckery#28).

AT-SPI2's VISIBLE/SHOWING window states are NOT virtual-desktop-aware (a
window on a completely different KWin desktop still reports VISIBLE=True,
SHOWING=True — apps have no concept of virtual desktops, that's a pure
compositor concept). Wayland deliberately exposes no public D-Bus call for
"list windows + which desktop they're on" either. The only place this data
exists is inside KWin's own scripting API, so this class owns
org.deckery.WindowTracker on the session bus (shared between host and the
deckery distrobox container) and receives live pushes from the companion
KWin script ~/.local/share/kwin/scripts/deckery-windowtracker, which calls
Update(payload_json) on windowAdded/windowRemoved/currentDesktopChanged/
minimizedChanged/desktopsChanged/frameGeometryChanged.

This module is pure state + D-Bus plumbing — it does not know about AT-SPI,
GTK drawing, or the actual tree walk. Callers (deckery_walk.py) read the
public attributes below to decide what to draw; deckery_atspi_overlay.py
wires the on_update callback to trigger a debounced re-walk.
"""

import json
import os
import time

from gi.repository import GLib, Gio


def _ts():
    """Millisecond wall-clock timestamp for correlating log lines across
    processes/modules when diagnosing timing-sensitive bugs (deckery#28) —
    cheaper than re-running with a debugger, and survives in scrollback."""
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


TRACKER_BUS_NAME = "org.deckery.WindowTracker"
TRACKER_OBJECT_PATH = "/WindowTracker"
TRACKER_INTERFACE_XML = """
<node>
  <interface name="org.deckery.WindowTracker">
    <method name="Update">
      <arg type="s" name="payload_json" direction="in"/>
    </method>
  </interface>
</node>
"""

# One-off snippet (deckery#28) that does exactly what the persistent
# deckery-windowtracker KWin script does on windowAdded/etc: pushes the
# current window list once. Triggered via KWin's generic, documented
# /Scripting D-Bus interface right after we own our own bus name, so a
# freshly-started tracker gets real data immediately instead of waiting for
# the NEXT natural KWin event (which could be seconds away or, if nothing
# happens to change, never) — confirmed live this was the cause of the
# overlay staying in unfiltered fallback mode for an entire session. Kept as
# a separate ad-hoc snippet rather than modifying the persistent script:
# KWin JS has no documented setInterval/Timer for "just poll periodically",
# and the persistent script is what carries all desktop-filtering for the
# whole session — safer to not touch it for this.
_KWIN_PUSH_ONCE_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools", "kwin_push_once.js"
)

# Debounce delay between a tracker Update() and triggering a re-walk. Short
# enough that a desktop switch feels responsive, long enough to coalesce the
# handful of KWin signals (windowAdded + frameGeometryChanged etc.) that can
# fire back-to-back for a single user action into one walk instead of many.
DEBOUNCE_MS = 150


class WindowTracker:
    """Owns org.deckery.WindowTracker and tracks live desktop-membership
    state pushed by the KWin script.

    Public attributes (read by deckery_walk.py / deckery_filter.py):
      visible_pids            -- set of pids visible on the current desktop
                                  right now. None until the first Update()
                                  arrives (tracker not running yet).
      visible_captions_by_pid -- {pid: set(caption)} for windows visible now
      all_captions_by_pid     -- {pid: set(caption)} for every window KWin
                                  has ever reported for that pid
      all_known_pids          -- set of every pid KWin has ever reported
      caption_visible_global  -- {caption: bool}, pid-independent fallback
                                  for sandboxed apps where AT-SPI's pid
                                  doesn't match KWin's real pid (see
                                  deckery_filter.py)
      caption_offset_global   -- {caption: (x, y)}, pid-independent
                                  fallback offset, same reasoning
      client_offset_by_key    -- {(pid, caption): (x, y)} on-screen offset
                                  to add to AT-SPI's window-relative extents
      client_offset_by_key_count -- {(pid, caption): int}, how many windows
                                  this payload reported under that exact key
                                  — resolve_offset() only trusts an entry in
                                  client_offset_by_key when this is 1 (see
                                  its own comment above)
      client_offset_by_pid    -- {pid: (x, y)}, fallback offset when the
                                  exact (pid, caption) key misses
      client_offset_by_pid_size -- {(pid, width, height): (x, y)}, fallback
                                  keyed on rounded window size instead of
                                  caption. Needed for apps like plasmashell
                                  where EVERY top-level window reports an
                                  empty caption — (pid, caption) collapses
                                  all of that app's panels/popups into one
                                  key, so whichever window KWin processed
                                  last in a given Update() silently
                                  overwrites the others' offsets in
                                  client_offset_by_pid. Window size is a much
                                  more distinguishing key for this case
                                  (deckery#28).
      hidden_pid_size_keys      -- set of (pid, width, height) for windows
                                  that KWin explicitly reported as hidden
                                  (auto-hide panels currently tucked away,
                                  i.e. slid off-screen). Uses the same
                                  pid+size key as client_offset_by_pid_size
                                  because plasmashell panels all have empty
                                  captions. Empty set if KWin doesn't expose
                                  the hidden flag (safe no-op in that case).
      active_pid                -- pid of the currently-focused window, or
                                  None until the first Update() arrives.
                                  Used by menu_badge_prototype.py to correct
                                  the Global Menu dropdown's position via the
                                  focused app's own (reliable) offset instead
                                  of kded6's own ambiguous anchor windows
                                  (deckery#28).
    """

    def __init__(self, on_update=None):
        # Called (no args) after each Update() has refreshed the state
        # above, debounced by DEBOUNCE_MS. Intended to trigger a re-walk.
        self._on_update = on_update

        self.visible_pids = None
        self.visible_captions_by_pid = {}
        self.all_captions_by_pid = {}
        self.all_known_pids = set()
        self.caption_visible_global = {}
        self.caption_offset_global = {}
        self.client_offset_by_key = {}
        self.client_offset_by_key_count = {}
        self.client_offset_by_pid = {}
        self.client_offset_by_pid_size = {}
        self.hidden_pid_size_keys = set()
        self.active_pid = None

        self._pending_refresh_id = None

    def start(self):
        Gio.bus_own_name(
            Gio.BusType.SESSION,
            TRACKER_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            None,
            self._on_name_lost,
        )

    def _on_bus_acquired(self, connection, name):
        node_info = Gio.DBusNodeInfo.new_for_xml(TRACKER_INTERFACE_XML)
        connection.register_object(
            TRACKER_OBJECT_PATH,
            node_info.interfaces[0],
            self._on_method_call,
            None,
            None,
        )
        print(f"[{_ts()}] [tracker] registered {TRACKER_OBJECT_PATH} on {name}, waiting for KWin updates...")
        self._request_initial_push(connection)

    def _request_initial_push(self, connection):
        """One-shot: ask KWin to run _KWIN_PUSH_ONCE_SCRIPT via its generic
        /Scripting interface, so we get real data immediately instead of
        waiting for the persistent script's next natural event (deckery#28
        — see _KWIN_PUSH_ONCE_SCRIPT's comment). Verified live: loadScript
        + run() on the returned Script<id> path reliably delivers exactly
        one Update() call within about a second. Best-effort — any failure
        here (KWin's Scripting interface unavailable, script path wrong,
        etc.) just means we fall back to the pre-existing "wait for the
        next natural event" behavior; never fatal."""
        try:
            (script_id,) = connection.call_sync(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                GLib.Variant("(ss)", (_KWIN_PUSH_ONCE_SCRIPT, "deckery-initial-push")),
                GLib.VariantType("(i)"), Gio.DBusCallFlags.NONE, 2000, None,
            ).unpack()
            connection.call_sync(
                "org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script", "run",
                None, None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            connection.call_sync(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                GLib.Variant("(s)", ("deckery-initial-push",)),
                GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None,
            )
            print(f"[{_ts()}] [tracker] requested an immediate push from KWin via /Scripting")
        except GLib.Error as e:
            print(f"[{_ts()}] [tracker] could not request initial push (non-fatal, will wait for next natural event): {e}")

    def _on_name_lost(self, connection, name):
        # Most likely another overlay instance is still running — harmless
        # for this prototype, just means we won't get live desktop-
        # membership data and the walk falls back to VISIBLE+SHOWING-only
        # filtering.
        print(f"[{_ts()}] [tracker] FAILED to own {name} — is another overlay instance still running?")

    def _on_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if method_name != "Update":
            invocation.return_value(None)
            return
        (payload_json,) = parameters.unpack()
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as e:
            print(f"[{_ts()}] [tracker] bad JSON from KWin script: {e}")
            invocation.return_value(None)
            return

        current = payload.get("currentDesktop")
        windows = payload.get("windows", [])
        self.active_pid = payload.get("activePid")
        visible_pids = set()
        # pid alone is too coarse for apps with several top-level KWin
        # windows on DIFFERENT desktops (e.g. System Settings with more than
        # one panel open) — matching by pid only would then show ALL of that
        # app's windows since the pid is "visible" via just one of them. So
        # also track per-pid caption sets: which captions are visible right
        # now vs which captions exist at all for that pid. deckery_filter.py
        # uses the caption as a tie-breaker when an AT-SPI window's name
        # matches a known KWin caption; falls back to pid-only when it
        # doesn't (AT-SPI sometimes reports sub-panes as separate top-level
        # frames with names KWin never sees, e.g. a "Sidebar" pane inside
        # one window).
        visible_captions_by_pid = {}
        all_captions_by_pid = {}
        # (pid, caption) -> (screen_x, screen_y) offset to add to AT-SPI's
        # window-relative extents. See prototype_atspi_scan.py's walk() and
        # the KWin script's clientGeometry comment for why this is needed:
        # Qt's AT-SPI bridge doesn't apply this display's rotation/scale
        # transform, so every element's reported position is relative to its
        # own window's client area, not the real screen.
        client_offset_by_key = {}
        # How many windows this payload reported under each (pid, caption)
        # key — lets resolve_offset() (deckery_filter.py) distinguish "this
        # key identifies exactly one real window" from "multiple windows
        # share this exact caption, the match is a coin flip." Needed
        # because an app's NAME isn't always a unique window title: shell
        # processes that host several popups under the SAME literal caption
        # (confirmed live, deckery#28: kded6's Global Menu anchors all
        # report caption='kded6'; plasmashell's many popups — tray flyouts,
        # the window-decoration/Window Menu popup, OSDs — all report
        # caption='plasmashell') would otherwise silently resolve to
        # whichever one of them this payload happened to process last,
        # landing menu badges hundreds of pixels off. Generic by design
        # (counts, doesn't name specific processes) so any future app with
        # the same pattern is caught automatically.
        client_offset_by_key_count = {}
        # Fallback when the exact (pid, caption) match misses (e.g. AT-SPI's
        # frame name differs slightly from KWin's caption for some app) —
        # last-seen offset for that pid alone. Better than defaulting to
        # (0, 0), which would draw at the raw window-relative coordinates
        # (visibly wrong: clustered near whatever those small numbers are).
        client_offset_by_pid = {}
        # Fallback keyed on (pid, rounded width, rounded height) instead of
        # caption. Needed for apps like plasmashell where EVERY top-level
        # window reports an empty caption ("") — client_offset_by_key and
        # client_offset_by_pid above then collapse ALL of that app's
        # panels/popups into the SAME key, so whichever window is processed
        # last in this loop silently overwrites the others' offsets. Window
        # size is a far more distinguishing key for this case: the top bar,
        # left panel, systray corner, and any flyout popup are all different
        # sizes. Rounded because KWin reports fractional geometry (e.g.
        # 66.36363...) while AT-SPI's frame extents come back as plain ints
        # (deckery#28).
        client_offset_by_pid_size = {}
        # Windows explicitly hidden by KWin (auto-hide panels currently tucked
        # away). Keyed on (pid, rounded_width, rounded_height) — same
        # mechanism as client_offset_by_pid_size because plasmashell panels
        # all have empty captions and can't be told apart by (pid, caption).
        # Empty if the KWin script doesn't push a "hidden" field (!!undefined
        # is false in JS, so old KWin builds/scripts send false for all
        # windows, meaning this set stays empty and callers see a safe no-op).
        hidden_pid_size_keys = set()
        # Sandboxed apps (Flatpak/bubblewrap, e.g. Firefox) break PID-based
        # matching entirely: AT-SPI's connection to a sandboxed app is routed
        # through the sandbox's xdg-dbus-proxy, so
        # Atspi.Accessible.get_process_id() returns the PROXY's PID, not the
        # real app PID that KWin sees on the compositor side. Confirmed by
        # comparing Firefox's AT-SPI-reported pid (118450 == xdg-dbus-proxy)
        # against KWin's queryWindowInfo for the same window (118575 ==
        # /app/lib/firefox/firefox) — completely different processes, so pid
        # matching can NEVER succeed for this window no matter which desktop
        # is active (deckery#28). Since caption is the only reliable signal
        # left in that case, also build a pid-independent caption->visible
        # map (and caption->offset map) as a fallback for when the AT-SPI pid
        # isn't among ANY pid KWin has ever reported (all_known_pids) — that
        # specific condition is what identifies "pid correlation is broken
        # for this window", as opposed to "pid is known but just not visible
        # right now" which should still filter normally.
        all_known_pids = set()
        caption_visible_global = {}
        caption_offset_global = {}
        # Smallest-window-wins tiebreak for client_offset_by_pid (below):
        # apps that host their own tiny on-screen anchor widget PLUS
        # separate popup/dropdown windows under the same pid (confirmed
        # live for kded6's Global Menu — a ~1x1px anchor at the real panel
        # button position, alongside the dropdown's own much larger popup
        # window(s), both pid-tagged identically) would otherwise have
        # client_offset_by_pid[pid] silently clobbered by whichever window
        # KWin happened to report last in this payload — observed live as
        # the SAME still-open menu's correction offset changing value
        # (55,59) -> (707,59) -> (820,59) across consecutive Update() calls,
        # each one coinciding with one of its own popup windows appearing in
        # workspace.windowList(). The anchor is reliably the smallest window
        # for that pid, so prefer it deterministically instead of "last one
        # wins" (deckery#28).
        # NOT smallest-area-wins (tried that, reverted — see below).
        # Correct tiebreak: prefer normalWindow=True (the actual top-level
        # app window) over any smaller transient helper window sharing the
        # same pid; among equally-ranked candidates, smallest area wins.
        #
        # Smallest-area-only was tried first for the kded6 Global Menu case
        # (a ~1x1px anchor placeholder alongside the dropdown's own much
        # larger popup window(s), both pid-tagged identically — "last one
        # wins" let the popup clobber the anchor's offset). But smallest-
        # area-ONLY is wrong in general: confirmed live for Konsole
        # (pid=13377) that it ALSO has two windows — its real main window
        # (caption 'user : bash — Konsole', large) and an unrelated
        # small transient helper window (empty caption, position changes
        # independently) — smallest-area picked the small transient one
        # EVERY time, silently overriding the real window's correct,
        # already-tracked offset with a wrong, drifting one (deckery#28).
        # normalWindow is reliably True for the real window and False for
        # both kded6's own anchor AND Konsole's transient helper, so
        # ranking on it first fixes both cases at once.
        client_offset_by_pid_rank = {}
        # Debug only (deckery#28): every (caption, width, height, area,
        # normalWindow) candidate seen per pid this payload, so a pid whose
        # client_offset_by_pid value changes between payloads can be
        # diagnosed from the log instead of guessed at.
        _debug_candidates_by_pid = {}
        for w in windows:
            pid = w.get("pid")
            if not pid or pid < 0:
                continue
            all_known_pids.add(pid)
            caption = w.get("caption", "")
            on_current = current in w.get("desktops", []) or w.get("onAllDesktops")
            # Deliberately NOT gated on w.get("normalWindow"): that flag is
            # False for Plasma panels, system trays, and the taskbar (they're
            # KWin "dock"-type surfaces, not "normal" application windows).
            # Requiring it here meant their pid never entered visible_pids,
            # so they were silently filtered out of the overlay every cycle
            # no matter which desktop was active — user explicitly wants
            # panels/tray/taskbar included, not treated as noise (deckery#28).
            visible_now = on_current and not w.get("minimized")
            all_captions_by_pid.setdefault(pid, set()).add(caption)
            # Prefer bufferGeometry over clientGeometry as the offset source:
            # for GTK/CSD apps (Firefox, GTK4 apps) AT-SPI's element-relative
            # coordinate origin is anchored to the surface BUFFER's top-left,
            # which includes an invisible shadow/resize-handle margin that
            # clientGeometry excludes (~26px left / ~23px top measured for
            # Firefox on this system). Using clientGeometry overshoots by
            # exactly that margin, pushing every rectangle down-and-right by
            # a constant amount — confirmed visually (deckery#28). Apps
            # without CSD (e.g. Dolphin) report bufferGeometry == clientGeometry
            # so this is a strict improvement, not a regression for them.
            geo = w.get("bufferGeometry") or w.get("clientGeometry")
            if geo:
                offset = (geo.get("x", 0), geo.get("y", 0))
                client_offset_by_key[(pid, caption)] = offset
                client_offset_by_key_count[(pid, caption)] = client_offset_by_key_count.get((pid, caption), 0) + 1
                area = geo.get("width", 0) * geo.get("height", 0)
                is_normal = bool(w.get("normalWindow"))
                rank = (0 if is_normal else 1, area)
                _debug_candidates_by_pid.setdefault(pid, []).append(
                    (caption, geo.get("width", 0), geo.get("height", 0), area, is_normal, offset)
                )
                if pid not in client_offset_by_pid_rank or rank < client_offset_by_pid_rank[pid]:
                    client_offset_by_pid[pid] = offset
                    client_offset_by_pid_rank[pid] = rank
                size_key = (pid, round(geo.get("width", 0)), round(geo.get("height", 0)))
                client_offset_by_pid_size[size_key] = offset
                if caption:
                    caption_offset_global[caption] = offset
                if w.get("hidden", False):
                    hidden_pid_size_keys.add(size_key)
            if caption:
                # last write wins if two different pids happen to share a
                # caption (rare) — good enough for a fallback path.
                caption_visible_global[caption] = caption_visible_global.get(caption, False) or visible_now
            if visible_now:
                visible_pids.add(pid)
                visible_captions_by_pid.setdefault(pid, set()).add(caption)

        self.visible_pids = visible_pids
        self.visible_captions_by_pid = visible_captions_by_pid
        self.all_captions_by_pid = all_captions_by_pid
        self.all_known_pids = all_known_pids
        self.caption_visible_global = caption_visible_global
        self.caption_offset_global = caption_offset_global
        self.client_offset_by_key = client_offset_by_key
        self.client_offset_by_key_count = client_offset_by_key_count
        self.client_offset_by_pid = client_offset_by_pid
        self.client_offset_by_pid_size = client_offset_by_pid_size
        self.hidden_pid_size_keys = hidden_pid_size_keys
        print(f"[{_ts()}] [tracker] update: currentDesktop={current}, visible_pids={sorted(visible_pids)}")
        for pid, candidates in _debug_candidates_by_pid.items():
            if len(candidates) > 1:
                print(f"[{_ts()}] [tracker] pid={pid} has {len(candidates)} windows sharing "
                      f"client_offset_by_pid — chose offset={client_offset_by_pid[pid]} "
                      f"(normalWindow first, smallest area tiebreak) from candidates={candidates}")

        # Trigger an immediate (debounced) re-walk instead of waiting for the
        # next periodic tick — otherwise the overlay keeps showing the
        # previous desktop's elements for a while after a desktop switch even
        # though we just found out it's stale.
        if self._pending_refresh_id is not None:
            GLib.source_remove(self._pending_refresh_id)
        self._pending_refresh_id = GLib.timeout_add(DEBOUNCE_MS, self._debounced_update)

        invocation.return_value(None)

    def _debounced_update(self):
        self._pending_refresh_id = None
        if self._on_update is not None:
            self._on_update()
        return False  # one-shot
