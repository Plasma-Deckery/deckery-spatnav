#!/usr/bin/env python3
"""
deckery_filter.py — visibility filtering + coordinate/scale correction
(deckery#28).

Pure logic, no GTK/cairo/Atspi drawing here: given a WindowTracker's live
state (deckery_tracker.py) plus one AT-SPI window's (pid, caption), decides
whether that window should be drawn right now, and by how much its raw
window-relative element extents need to be offset/scaled to land at the
right place on screen. deckery_walk.py calls into this for every window/
element it visits.
"""

# AT-SPI's WINDOW-relative x/y/width/height are reported in the app
# toolkit's own logical/DIP pixel space, but the compositor-scaled logical
# space this GTK4 layer-shell surface draws into (and that KWin's
# clientGeometry offset is expressed in) is smaller by this display's
# fractional Scale factor (kscreen-doctor -o: Scale 1.1 on this system's
# panel). Net effect measured empirically (deckery#28): boxes near a
# window's own top-left (local 0,0) land correctly since 0 * anything is
# still 0, but drift increasingly right/down the further an element is
# from that corner — the classic signature of local coordinates being
# ~1.1x too large relative to what this compositor-scaled surface expects.
# Dividing local extents by DISPLAY_SCALE before adding the (already
# correctly-scaled) KWin offset corrects this. Hardcoded rather than
# queried at runtime because the distrobox container the overlay runs in
# has no kscreen-doctor/qdbus to read it back (see ATSPI_LIMITATIONS.md's
# single-monitor assumption) — revisit if this ever needs to be dynamic
# (e.g. by having the KWin tracker script push the output scale alongside
# clientGeometry, since it already has host-side KWin JS API access).
#
# Whether to apply this at all: measured empirically (deckery#28) that this
# local-coordinate scale mismatch is Chromium-specific — Chromium's
# WebContents accessibility tree reports element positions in CSS/DIP
# pixels, distinct from the compositor-scaled logical space every other
# toolkit's AT-SPI bridge already uses. Applying the /DISPLAY_SCALE
# correction to Qt (System Settings) or Gecko (Firefox) windows made their
# (already-correct) positions wrong, confirming this — see
# is_chromium_toolkit() / needs_scale_correction() below, and
# _do_walk()'s per-app toolkit_name lookup in deckery_walk.py.
DISPLAY_SCALE = 1.1

# Native window-frame buttons (Minimize/Maximize/Restore/Close), even within
# a Chromium app, are drawn by Chromium's own custom titlebar code, not the
# web renderer — they're already reported in the compositor-scaled space.
# Confirmed empirically (deckery#28): applying the DISPLAY_SCALE correction
# to these buttons too made them land wrong while everything else became
# correct.
CHROME_WINDOW_BUTTON_NAMES = ("Minimize", "Maximize", "Restore", "Close")


def is_chromium_toolkit(toolkit_name):
    # Case-insensitive: vendor/orca_ax's AXObject.get_toolkit_name() (used by
    # deckery_walk.py as of deckery#28's Orca-vendoring step) lowercases its
    # result, unlike the raw Atspi.Accessible.get_toolkit_name() call this
    # replaced (which returned "Chromium", capitalized).
    return (toolkit_name or "").lower() == "chromium"


def needs_scale_correction(is_chromium_app, element_name):
    return is_chromium_app and element_name not in CHROME_WINDOW_BUTTON_NAMES


def compute_offset_and_scale(extents, is_chromium_app, element_name, offset_x, offset_y):
    """Returns a NEW (x, y, width, height) tuple: raw window-relative
    `extents` with the on-screen window offset added, and — only for
    Chromium WebContents-derived elements — local coordinates divided by
    DISPLAY_SCALE first. See the DISPLAY_SCALE / CHROME_WINDOW_BUTTON_NAMES
    comments above for why the split exists.

    Deliberately non-mutating (unlike the apply_offset_and_scale() this
    replaced, which mutated `extents` in place): as of deckery#28's
    event-driven rewrite, `extents` here is the RAW, cached, AT-SPI-tree-
    walk result held in deckery_events.TreeCache — the same cached Rect gets
    re-rendered on every tracker update (desktop switch, window move) without
    a new AT-SPI walk. Mutating it in place would keep re-adding the offset
    on every render call, compounding it further from the real position each
    time."""
    if needs_scale_correction(is_chromium_app, element_name):
        x = round(extents.x / DISPLAY_SCALE + offset_x)
        y = round(extents.y / DISPLAY_SCALE + offset_y)
        width = round(extents.width / DISPLAY_SCALE)
        height = round(extents.height / DISPLAY_SCALE)
    else:
        x = extents.x + offset_x
        y = extents.y + offset_y
        width = extents.width
        height = extents.height
    return x, y, width, height


# AT-SPI apps whose own VISIBLE/SHOWING window states are known to be
# unreliable and should NOT gate whether a frame gets drawn. Currently just
# KDE's own shell: plasmashell's panels are fine (always report
# VISIBLE=True/SHOWING=True correctly), but its popups/flyouts (app
# launcher, systray flyouts, clipboard popup, notification popups, etc.)
# are QML surfaces where these states get stuck — confirmed empirically
# (deckery#28) by comparing state snapshots of the same flyout open vs.
# closed: one flyout's frame stayed VISIBLE=False/SHOWING=False the entire
# time it was visibly open on screen (no ACTIVE/FOCUSED state appeared
# either — there is no reliable AT-SPI signal at all for this window), a
# different flyout stayed stuck at VISIBLE=True/SHOWING=True even while
# closed. KWin's own workspace.windowList() doesn't track these popups
# either (confirmed via a live D-Bus capture of the KWin tracker script's
# payload while a flyout was open — none of plasmashell's popup frames
# appeared, only its 5 real panels did), so there is no alternative
# visibility signal to fall back on from that side either.
UNRELIABLE_VISIBILITY_APPS = ("plasmashell",)


def has_unreliable_visibility_state(app_name):
    return app_name in UNRELIABLE_VISIBILITY_APPS


def frame_has_valid_extents(extents):
    """Least-bad visibility proxy for apps in UNRELIABLE_VISIBILITY_APPS:
    draw any frame that has ever been laid out with a real size, regardless
    of its (untrustworthy) VISIBLE/SHOWING state. This means a closed
    flyout can still show a stale box in its last-known position/size
    until it's opened again — the same behavior plasmashell's "stuck-True"
    popups already exhibited by accident, which was confirmed acceptable
    (box lands exactly right once the flyout is actually open) — just now
    applied consistently so "stuck-False" popups draw too instead of never
    appearing at all (deckery#28)."""
    return extents is not None and extents.width > 0 and extents.height > 0


def is_window_visible(tracker, pid, caption):
    """Cross-references (pid, caption) against the KWin tracker's live
    visible-on-current-desktop data. Returns True/False, or True if the
    tracker hasn't received any data yet (self.visible_pids is None) —
    better to show too much than nothing while the KWin script/pipeline
    isn't up yet."""
    if tracker.visible_pids is None:
        return True

    pid_correlation_broken = pid not in tracker.all_known_pids
    if pid_correlation_broken:
        # AT-SPI's pid doesn't match ANY pid KWin has ever reported — not
        # "known but not visible right now", but pid matching itself is
        # broken for this window. Happens for sandboxed apps (Flatpak/
        # bubblewrap): AT-SPI's connection is routed through the sandbox's
        # xdg-dbus-proxy, so get_process_id() returns the PROXY's pid, not
        # the real app pid KWin sees on the compositor side (confirmed for
        # Firefox: AT-SPI reported 118450 == xdg-dbus-proxy, KWin reported
        # 118575 == the actual firefox binary — deckery#28). Caption is the
        # only signal left in that case.
        return tracker.caption_visible_global.get(caption, False)

    if pid not in tracker.visible_pids:
        return False

    # pid alone isn't enough for apps with several top-level KWin windows
    # spread across DIFFERENT desktops (e.g. System Settings with more than
    # one panel open) — pid membership only proves *some* window of that app
    # is visible, not this one. Use the AT-SPI frame's name as a caption
    # tie-breaker: if it matches a caption KWin knows about for this pid,
    # trust KWin's per-window visibility instead of the pid-wide fallback.
    # If it doesn't match anything KWin reported (AT-SPI sometimes exposes
    # internal sub-panes as separate top-level frames the KWin script never
    # sees, e.g. a "Sidebar" pane), fall back to the pid-wide visible flag
    # already checked (True at this point).
    known_captions = tracker.all_captions_by_pid.get(pid, set())
    if caption in known_captions:
        visible_captions = tracker.visible_captions_by_pid.get(pid, set())
        return caption in visible_captions
    return True


def is_explicitly_hidden(tracker, pid, frame_width, frame_height):
    """Returns True if KWin explicitly reported this window as hidden — i.e.
    an auto-hide panel that is currently tucked away (slid off-screen) rather
    than being visible. This is distinct from minimized (which means "in the
    taskbar") and from being on another desktop.

    Uses the pid+size key (same as client_offset_by_pid_size) because
    plasmashell panels all report empty captions, making (pid, caption)
    useless for per-window discrimination. Returns False if the tracker
    hasn't received any hidden-state data from the KWin script (safe no-op
    for older KWin builds or if the KWin script doesn't push a "hidden"
    field — the set just stays empty)."""
    if frame_width is None or frame_height is None:
        return False
    size_key = (pid, round(frame_width), round(frame_height))
    return size_key in tracker.hidden_pid_size_keys


def resolve_offset(tracker, pid, caption, frame_width=None, frame_height=None):
    """Returns (offset_x, offset_y, is_fallback). AT-SPI's extents are
    window-client-relative on this system (see prototype_atspi_scan.py), so
    the real on-screen offset from the KWin tracker must be added back in.

    Try an exact (pid, caption) match first — but only when caption is
    non-empty AND unique for this pid this payload (client_offset_by_key_count
    == 1). Two ways a caption can fail to identify a single window:
    - empty (e.g. plasmashell — every one of its panels/popups reports "" as
      its AT-SPI name, so (pid, "") would collapse them all into the SAME
      key and only the last-processed one's offset would survive);
    - non-empty but still shared by several windows of the same pid
      (confirmed live, deckery#28: kded6's Global Menu anchors AND
      plasmashell's own window-decoration/Window Menu popup both report the
      literal app name — 'kded6', 'plasmashell' — as their caption, same
      collapsing problem, just not caught by an empty-string check).
    Either way, fall through to a size-keyed match instead when the caller
    passed the window's own frame size — much more distinguishing for
    same-pid/same-caption windows (deckery#28). Bug fixed here (deckery#28,
    live top-taskbar/left-panel regression): this guard used to only check
    for an empty caption, so the ambiguous (pid, "") key in
    client_offset_by_key was checked — and matched — BEFORE the size-keyed
    fallback ever got a chance to run, meaning every same-captioned window of
    a multi-window app/shell-process silently got whichever one of them
    happened to be walked/recorded last (confirmed live: all of plasmashell's
    panels were drawn at the bottom bar's offset; separately, a
    plasmashell-hosted popup menu landed hundreds of pixels off using
    whichever other plasmashell surface was processed last). Then fall back
    to any offset known for that pid at all (AT-SPI's frame name can differ
    slightly from KWin's caption, or be a sub-pane KWin never sees — for
    ambiguous-caption multi-window apps this is only a last resort since it
    inherits the same collapsing problem); then the pid-independent
    caption-only map (sandboxed apps where AT-SPI's pid is the
    xdg-dbus-proxy, not the real app pid — see is_window_visible above);
    only (0, 0) — drawn at the wrong position — if we know nothing about
    this window at all yet."""
    key = (pid, caption)
    if caption and tracker.client_offset_by_key_count.get(key, 0) == 1 and key in tracker.client_offset_by_key:
        return (*tracker.client_offset_by_key[key], False)
    if frame_width is not None and frame_height is not None:
        size_key = (pid, round(frame_width), round(frame_height))
        if size_key in tracker.client_offset_by_pid_size:
            return (*tracker.client_offset_by_pid_size[size_key], False)
    if pid in tracker.client_offset_by_pid:
        return (*tracker.client_offset_by_pid[pid], False)
    if caption in tracker.caption_offset_global:
        return (*tracker.caption_offset_global[caption], True)
    return (0, 0, True)
