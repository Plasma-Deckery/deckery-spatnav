"""deckery_event_filter.py — event-flood filtering for AT-SPI event listeners.

Not a straight copy of anything: this started as an attempt to lift
EventManager._ignore_by_spam_filter() / _ignore_children_changed() / etc.
verbatim out of GNOME Orca's event_manager.py (see vendor/orca_ax/), but on
closer inspection those methods aren't cleanly separable — they call into
Orca's full AXUtilities facade (ax_table/ax_text/ax_hypertext/every
ax_utilities_*.py) and into script_manager.get_active_script() (Orca's
per-app "Script" dispatch concept, which we have no equivalent of and don't
want). Vendoring them as-is would have meant vendoring most of Orca's
runtime just to reuse a handful of filtering *rules*.

So instead this module reimplements the specific rules that are directly
useful for deckery's overlay (dedup floods, drop known-spammy sources) as
plain functions against our own vendored vendor/orca_ax/ax_object.py, using
raw Atspi.Role checks instead of the AXUtilities facade. Credit where due:
the rules themselves — which event types are spammy, which roles/sizes are
safe to ignore — are Orca's hard-won knowledge (see
event_manager.py:_ignore_by_spam_filter, :_ignore_children_changed,
:_ignore_state_changed_subtype, :_ignore_property_change,
:_ignore_text_events in the Orca source), just reimplemented against a
smaller dependency surface. Not maintained by upstream Orca — if their
filtering rules improve, this needs a manual re-sync, unlike vendor/orca_ax/
which can mostly be dropped in wholesale from a newer Orca checkout.

Deliberately dropped vs. Orca's version:
  - No "obsoleted by newer queued event of same type" dedup (_is_obsoleted_by)
    — that assumes Orca's own priority event queue, which deckery doesn't
    have (yet). Revisit if/when deckery_walk.py grows an event queue instead
    of the current re-walk-on-signal model.
  - No focus-state carve-outs (_ignore_by_focus_state) — those exist so Orca
    doesn't suppress events about whatever the user is currently interacting
    with, which is a speech-output concern. deckery's overlay wants to know
    about *any* visible focusable element changing, not just the currently
    focused one.
  - No script-manager-gated children-changed filtering ("ignore unless from
    the active script's app") — deckery walks every visible window on every
    virtual desktop, not just one "active" app, so that rule doesn't apply.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from vendor.orca_ax.ax_object import AXObject

# Same threshold Orca uses (event_manager.py:_ignore_by_spam_filter): if the
# same event type fires twice for the same application within this window,
# the second one is almost certainly redundant UI churn, not a new distinct
# change worth re-walking the tree for.
_DEDUP_WINDOW_SECONDS = 0.1

# Same threshold Orca uses (event_manager.py:_ignore_by_spam_filter) to drop
# X11-frame-shim churn: under XWayland, Mutter (and, empirically, KWin's
# XWayland compatibility layer too) emits large numbers of synthetic
# X11-frame accessibility events that don't correspond to real UI changes.
_MUTTER_X11_FRAMES_APP_NAME = "mutter-x11-frames"

# Same thresholds Orca uses (event_manager.py:_ignore_by_spam_filter /
# :_ignore_text_events) for "this text insertion/deletion is big enough that
# it's probably a bulk content load, not an incremental edit worth reacting
# to individually — a following state-changed/children-changed event on the
# container will trigger a re-walk anyway."
_IGNORE_TEXT_INSERT_CHARS = 1000
_IGNORE_TEXT_INSERT_CHARS_TEXT_ROLE = 5000

# Roles for which a "showing" state-changed event is worth reacting to.
# Everything else showing/hiding (most panels, containers, etc.) fires this
# constantly as a side effect of normal layout and isn't a real
# appear/disappear worth a re-walk on its own -- see
# event_manager.py:_ignore_state_changed_subtype.
#
# Window-level roles (FRAME, DIALOG, WINDOW, POPUP_MENU) are always
# interesting: a popup/dialog appearing or disappearing is exactly what the
# overlay needs to react to. Orca's original allowlist was speech-output
# oriented and didn't include FRAME (Orca learns about windows via
# window:create/destroy, not state-changed). Deckery adds them so that
# popups whose AT-SPI window lifecycle events don't fire (e.g. KDE plasmashell
# popups that use show/hide instead of create/destroy) still trigger a re-walk.
_SHOWING_STATE_INTERESTING_ROLES = (
    Atspi.Role.ALERT,
    Atspi.Role.ANIMATION,
    Atspi.Role.DIALOG,
    Atspi.Role.FRAME,
    Atspi.Role.INFO_BAR,
    Atspi.Role.MENU,
    Atspi.Role.NOTIFICATION,
    Atspi.Role.POPUP_MENU,
    Atspi.Role.STATUS_BAR,
    Atspi.Role.TOOL_TIP,
    Atspi.Role.WINDOW,
)


class EventSpamFilter:
    """Tracks recent event history to drop duplicate/flood AT-SPI events.

    One instance should be reused across the lifetime of the event listener
    (it holds per-event-type dedup state) -- don't recreate it per event.
    """

    def __init__(self) -> None:
        self._last_seen: dict[str, tuple[int | None, float]] = {}

    def should_ignore(self, event: Atspi.Event) -> bool:
        """Returns True if this event is flood/spam and safe to drop."""

        if _is_duplicate_within_window(event, self._last_seen):
            return True
        if _is_from_mutter_x11_frames(event):
            return True
        if _is_oversized_text_insert(event):
            return True
        if _is_uninteresting_children_changed(event):
            return True
        if _is_uninteresting_state_changed(event):
            return True
        return False


def _get_application_name(obj: Atspi.Accessible | None) -> str | None:
    """Best-effort application name lookup, mirroring AXObject's defensive style."""

    if obj is None:
        return None
    try:
        app = Atspi.Accessible.get_application(obj)
        if app is None:
            return None
        return Atspi.Accessible.get_name(app)
    except Exception:  # noqa: BLE001 - AT-SPI calls can fail in many toolkit-specific ways
        return None


def _is_duplicate_within_window(
    event: Atspi.Event, history: dict[str, tuple[int | None, float]]
) -> bool:
    """Drops repeats of the same event type from the same app within _DEDUP_WINDOW_SECONDS.

    Adapted from event_manager.py:_ignore_by_spam_filter -- the "same app,
    same event type, <0.1s apart" half of that method (the other half,
    checking against Orca's own priority event queue, doesn't apply to us).
    """

    app_name = _get_application_name(event.source)
    key = event.type
    last_app, last_time = history.get(key, (None, 0.0))
    now = time.time()
    ignore = last_app == app_name and (now - last_time) < _DEDUP_WINDOW_SECONDS
    history[key] = (app_name, now)
    return ignore


def _is_from_mutter_x11_frames(event: Atspi.Event) -> bool:
    """Drops synthetic XWayland frame-shim events (event_manager.py:_ignore_by_spam_filter)."""

    return _get_application_name(event.source) == _MUTTER_X11_FRAMES_APP_NAME


def _is_oversized_text_insert(event: Atspi.Event) -> bool:
    """Drops bulk text insertions (event_manager.py:_ignore_by_spam_filter/_ignore_text_events)."""

    if not event.type.startswith("object:text-changed:insert"):
        return False
    role = AXObject.get_role(event.source)
    limit = (
        _IGNORE_TEXT_INSERT_CHARS_TEXT_ROLE
        if role == Atspi.Role.TEXT
        else _IGNORE_TEXT_INSERT_CHARS
    )
    return event.detail2 > limit


def _is_uninteresting_children_changed(event: Atspi.Event) -> bool:
    """Drops children-changed noise with a null/dead child (event_manager.py:_ignore_children_changed).

    Note: deliberately does NOT port the "ignore unless from the active
    script's app" rule -- deckery has no "active app" concept, it tracks
    every visible window.
    """

    if not event.type.startswith("object:children-changed"):
        return False
    child = event.any_data
    if child is None:
        return True
    return AXObject.is_dead(child)


def _is_uninteresting_state_changed(event: Atspi.Event) -> bool:
    """Drops state-changed events for roles unlikely to matter for spatial nav.

    Adapted from event_manager.py:_ignore_state_changed_subtype -- keeps
    only the "showing" role-allowlist rule, which maps most directly onto
    "does an overlay-relevant element just appear/disappear". The
    checked/selected/sensitive sub-rules in Orca's version are about
    deciding whether something is speech-announcement-worthy, which isn't
    our concern -- we re-walk on any state change for those, and rely on
    the dedup window above to keep that cheap.
    """

    if not event.type.startswith("object:state-changed:showing"):
        return False
    role = AXObject.get_role(event.source)
    return role not in _SHOWING_STATE_INTERESTING_ROLES
