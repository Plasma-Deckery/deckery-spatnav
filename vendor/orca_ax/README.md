# vendor/orca_ax — trimmed AT-SPI access layer from GNOME Orca

These four files are copied, with light trims, from [GNOME Orca](https://gitlab.gnome.org/GNOME/orca)
(`src/orca/`), commit `e717975f915e66348646c010d2ddd11b433e524e` (2026-07-08).
Orca is licensed LGPL-2.1-or-later; each file below keeps its original
copyright/license header.

## Why these files and not the rest of Orca

deckery needs robust, non-blocking AT-SPI2 tree access — the exact same
problem Orca (GNOME's screen reader) solves, just for a different consumer
(a gamepad spatial-nav overlay instead of speech/braille output). Rather
than re-discover the same toolkit-specific edge cases Orca has already
hardened against (see e.g. the Chromium coordinate-scale bug we hit and
fixed independently in `deckery_filter.py` before finding this), we vendor
Orca's pure AT-SPI access/cache layer and write our own navigation logic on
top of it.

We deliberately did **not** pull in Orca's `event_manager.py` or
`focus_manager.py` wholesale: importing either one transitively drags in
Orca's whole application framework (`script_manager` → the entire
`scripts/apps/` per-application override directory, `braille_presenter` →
`orca_platform.tablesdir`, which assumes a real Orca install, `dbus_service`,
`systemd`, etc.) — none of which we want or need, and some of which may not
even import cleanly outside a full Orca installation. See the deckery
session notes for the fuller investigation. The event-flood-filtering
*rules* from `event_manager.py` were reimplemented (not copied) against
this smaller surface in `../../deckery_event_filter.py`.

## Files

- `debug.py` — Orca's debug-logging module. Trimmed: `print_tokens()` /
  `_stack_as_string()` upstream pretty-print `Atspi.Accessible` tokens via
  `ax_utilities_debugging.AXUtilitiesDebugging`, which pulls in three more
  modules (`ax_action.py`, `ax_utilities_application.py`,
  `ax_utilities_relation.py`) purely for nicer log formatting. Replaced with
  plain `str()` formatting instead of growing the dependency graph for a
  cosmetic feature.
- `ax_cache_manager.py` — generic, owner-scoped caching layer used by the
  other modules. Unmodified.
- `ax_object.py` — defensive wrapper around `Atspi.Accessible` (safe
  `get_name`/`get_role`/`get_state_set`/etc., with exception handling so one
  hung/dead accessible can't take down a whole tree walk). Unmodified.
- `ax_component.py` — wrapper around `Atspi.Component` (position/size/
  extents/hit-testing). Unmodified. Note: this does **not** fix deckery's
  Chromium DPI-scale bug on its own — it uses the same
  `Atspi.CoordType.WINDOW` we already use; the /1.1 Chromium-only
  correction in `deckery_filter.py` is still needed on top of this.
- `ax_utilities_state.py` — FOCUSABLE/SHOWING/ENABLED/etc. state-set
  queries. Trimmed: dropped `get_current_item_status_string()`, which
  mapped state to a translatable, speech-oriented string sourced from
  Orca's ~2700-line `messages.py` i18n module — a speech-output concern we
  have no equivalent of.

## Re-syncing from upstream

To pick up upstream fixes: diff these four files against the same paths in
a fresh Orca checkout, reapply the two trims above (search this repo for
"deckery vendor trim"), and re-check whether `event_manager.py`'s filtering
rules changed in ways worth re-porting into `deckery_event_filter.py`.
