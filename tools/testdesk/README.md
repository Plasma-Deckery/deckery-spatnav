# Test desktop

An invisible, isolated KDE desktop for testing the v2 backend automatically:
nested `kwin_wayland --virtual` with its own session D-Bus (→ own AT-SPI bus,
own KWin scripting) and its own config/data/cache dirs (wiped on start), so
nothing touches the real desktop or its settings. Deck-like by default:
1280x800 physical at scale 1.1 (1164x727 logical).

    tools/testdesk/restart_all.sh systemsettings dolphin   # desktop + apps + backend + viewer
    tools/testdesk/restart_all.sh tools/testdesk/gtk_testapp.sh dolphin
    tools/testdesk/run_box.sh python3 tools/testdesk/scenarios.py [name ...]
    tools/testdesk/run_box.sh python3 tools/testdesk/ctl.py check
    tools/testdesk/stop.sh                                 # (on the host)

Screenshots: `$XDG_RUNTIME_DIR/deckery-testdesk/shots/`, logs next to it.

A second, independent instance (own socket `wayland-deckery-test2`, state dir
`deckery-testdesk2`, session bus) runs next to the first when every command
gets the same suffix: `DECKERY_TESTDESK=2 tools/testdesk/restart_all.sh …`,
`DECKERY_TESTDESK=2 tools/testdesk/run_box.sh …`, and on the host
`env DECKERY_TESTDESK=2 tools/testdesk/stop.sh`.

- `start.sh` / `session.sh` / `stop.sh` — lifecycle (host). The session starts
  `at-spi-bus-launcher` and `at-spi2-registryd` itself (their D-Bus/systemd
  activation belongs to the real session) and sets the output scale with
  kscreen-doctor (`--virtual` ignores `--scale` for the output).
- `run.sh CMD` — host command inside the session; `run_box.sh CMD` — same in
  the deckery distrobox (Atspi/GTK4 bindings).
- `ctl.py` — move/raise windows (KWin), find/act on AT-SPI objects, read the
  backend's published targets, invariant checks (occlusion, freshness against
  live AT-SPI, ids, target points).
- `eis_input.py` — real pointer/keyboard input via KWin's EIS
  (`org.kde.KWin.EIS.RemoteDesktop`, libei through ctypes), reaching only the
  test desktop. Needed where Qt offers no AT-SPI action (context menus).
- `scenarios.py` — occlusion, move, scroll (far + small), context menu,
  virtual desktops, invariants; `gtk_*` on the GTK test app; `*_select`
  (open `<select>`) in the browsers.
- `gtk_testapp.py` / `gtk_testapp.sh` — GTK4 app with known content
  (every common control with a fixed name, disabled and hidden ones, three
  scroll containers, menu button, popover, drop-down, modal dialog, link).
  `gtk_testapp.sh` (re)starts it in the test desktop (distrobox, PID
  file); on the real desktop: `tools/run_desktop.sh testapp`.
- `mode_probe.py` — measures when the signals for special modes (focus,
  expanded, popup/dialog windows, backend update) arrive after real input,
  per app (`gtk`, `dolphin`, `systemsettings`, `chromium`, `firefox`);
  results in BACKEND_V2_DESIGN.md §12. Browsers with `controls.html`:
  `CHROMIUM_PAGE=controls.html tools/testdesk/chromium.sh`,
  `FIREFOX_PAGE=controls.html tools/testdesk/firefox.sh`.

Flatpak apps use the real `~/.var/app` profile even here — don't run
Firefox/Chromium Flatpaks in tests directly. `chromium.sh` (host) starts the
Chromium Flatpak with a throwaway profile under
`~/.var/app/org.chromium.Chromium/deckery-testdesk/` and `page.html`, with the
flags it needs to expose anything to AT-SPI (see the script).
`restart_backend.sh` restarts only backend + viewer (by PID file, never
pkill — the containers share the host's PID namespace). With
`DECKERY_READER=compare` (also for `restart_all.sh`) the backend reads every
window with both the batched and the libatspi reader and logs `[compare]`
differences and `/sync=…ms` timings; `=sync` runs the old reader alone.
