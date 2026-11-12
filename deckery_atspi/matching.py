"""KWin window ↔ AT-SPI frame matching (pure).

Each rule is named and logged with the match, so a wrong box can be traced
to the rule that produced it. Ambiguity is never resolved by guessing: an
ambiguous window stays unmatched and is not drawn (BACKEND_V2_DESIGN.md §6).

Facts this relies on (live dump 2026-11-10, tests/fixtures/*_mixed.json):
- AT-SPI frame size equals KWin's client size (Qt, GTK, Gecko, Steam's
  Chromium) or buffer size (Chromium with client-side titlebar, e.g.
  Claude 1182x732 vs buffer 1181.8x731.8), within rounding.
- Flatpak apps report the xdg-dbus-proxy pid to AT-SPI, not the app pid
  KWin sees (Trayscale 15847 vs 16060, Firefox 16027 vs 16205).
- Captions differ in whitespace ("Quick Settings  — System Settings" vs
  KWin's single space).
- One pid can own many same-role frames (Steam: 13, plasmashell: 15 incl.
  closed popups), so pid alone never identifies a frame."""

from dataclasses import dataclass

SIZE_TOLERANCE = 2.0


@dataclass(frozen=True)
class Match:
    frame_index: int
    rule: str


def _norm_caption(text):
    return " ".join((text or "").split())


def _size_matches(frame, window):
    for geo in (window.client, window.buffer):
        if geo is not None and abs(frame.width - geo.w) <= SIZE_TOLERANCE \
                and abs(frame.height - geo.h) <= SIZE_TOLERANCE:
            return True
    return False


def _pick(candidates, window, frames, rule_prefix):
    """Narrow candidates by size, then caption, then SHOWING; return a Match
    only if exactly one is left at some stage."""
    caption = _norm_caption(window.caption)
    sized = [i for i in candidates if _size_matches(frames[i], window)]
    if len(sized) == 1:
        return Match(sized[0], f"{rule_prefix}+size")
    pool = sized or candidates
    if caption:
        titled = [i for i in pool if _norm_caption(frames[i].name) == caption]
        if len(titled) == 1:
            return Match(titled[0], f"{rule_prefix}+{'size+' if sized else ''}caption")
        if titled:
            pool = titled
    if sized:
        showing = [i for i in pool if frames[i].showing]
        if len(showing) == 1:
            return Match(showing[0], f"{rule_prefix}+size+showing")
    return None


def match_windows(windows, frames):
    """windows: scene.Window list; frames: FrameInfo-like objects (pid,
    name, width, height, showing). Returns ({window.id: Match},
    {window.id: reason}) — the second dict lists unmatched windows."""
    kwin_pids = {w.pid for w in windows}
    used = set()
    matches = {}
    unmatched = {}

    def free(indices):
        return [i for i in indices if i not in used]

    # Same-pid windows first, so sandbox fallback only sees frames no
    # same-pid window could claim. Within a pid, larger windows first:
    # a big main window is least likely to be confused with its popups.
    ordered = sorted(windows, key=lambda w: -(w.frame.area if w.frame else 0))
    for window in ordered:
        candidates = free(i for i, f in enumerate(frames) if f.pid == window.pid)
        if not candidates:
            continue
        match = _pick(candidates, window, frames, "pid")
        if match:
            matches[window.id] = match
            used.add(match.frame_index)
        else:
            unmatched[window.id] = f"ambiguous among {len(candidates)} same-pid frames"

    for window in ordered:
        if window.id in matches or window.id in unmatched:
            continue
        # Sandbox matches must carry the caption: size alone across all
        # foreign-pid frames is too weak.
        caption = _norm_caption(window.caption)
        candidates = free(i for i, f in enumerate(frames)
                          if f.pid not in kwin_pids and caption and _norm_caption(f.name) == caption)
        match = _pick(candidates, window, frames, "sandbox+caption") if candidates else None
        if match is None and len(candidates) == 1:
            match = Match(candidates[0], "sandbox+caption")
        if match:
            matches[window.id] = match
            used.add(match.frame_index)
        else:
            unmatched[window.id] = "no same-pid frame, no unique caption among sandboxed frames"
    return matches, unmatched


def pick_popover(area, groups, tolerance):
    """The popup read into a main window that a KWin popup window shows:
    groups is {popup id: [screen rects of its elements]} (RawElement.popup,
    placed like the main window), area the popup window. The group whose
    elements all lie inside area (± tolerance) wins, the largest one if
    several do; None if none does."""
    def within(r):
        return (r.x >= area.x - tolerance and r.y >= area.y - tolerance
                and r.x2 <= area.x2 + tolerance and r.y2 <= area.y2 + tolerance)
    fits = [(len(rects), popup_id) for popup_id, rects in groups.items()
            if rects and all(within(r) for r in rects)]
    return max(fits)[1] if fits else None
