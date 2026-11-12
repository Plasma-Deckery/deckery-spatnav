"""Scenario checks for the v2 backend on the test desktop.

Run inside the test session (after tools/testdesk/restart_all.sh with the
apps a scenario needs):
    tools/testdesk/run_box.sh python3 tools/testdesk/scenarios.py [name ...]

Each scenario drives windows via KWin and widgets via AT-SPI, then asserts
on what the backend publishes. Screenshots land in
$XDG_RUNTIME_DIR/deckery-testdesk/shots/<scenario>-<step>.png for review."""

import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ctl
from deckery_atspi.geometry import Rect, region_area

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTANCE = os.environ.get("DECKERY_TESTDESK", "")
SHOTS = os.path.join(os.environ["XDG_RUNTIME_DIR"], f"deckery-testdesk{INSTANCE}", "shots")
# distrobox-host-exec does not forward the environment.
HOST_RUN = ["distrobox-host-exec", "env", f"DECKERY_TESTDESK={INSTANCE}",
            os.path.join(REPO, "tools/testdesk/run.sh")]


class ScenarioFailed(AssertionError):
    pass


def expect(condition, message):
    if not condition:
        raise ScenarioFailed(message)


def screenshot(name):
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, f"{name}.png")
    subprocess.run([*HOST_RUN,
                    "spectacle", "-b", "-n", "-f", "-o", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return path


def settle(timeout_s=4.0):
    """Wait until the backend has stopped publishing for a moment."""
    version, items = ctl.elements()
    deadline = time.monotonic() + timeout_s
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.25)
        v, i = ctl.elements()
        if v != version:
            version, items = v, i
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since > 0.8:
            break
    return version, items


def window_frame(cls):
    for w in reversed(ctl.kwin_eval(ctl._WINDOWS_JS)):
        if w["cls"] == cls and w["normal"]:
            return Rect(*w["frame"])
    raise ScenarioFailed(f"no window of class {cls}")


def has_window_class(cls):
    return any(w["cls"] == cls for w in ctl.kwin_eval(ctl._WINDOWS_JS))


def has_window(cls):
    return any(w["cls"] == cls and w["normal"] for w in ctl.kwin_eval(ctl._WINDOWS_JS))


def window_id(cls):
    for w in reversed(ctl.kwin_eval(ctl._WINDOWS_JS)):
        if w["cls"] == cls and w["normal"]:
            return w["id"]
    raise ScenarioFailed(f"no window of class {cls}")


def targets_in(items, rect):
    """Targets with a visible part intersecting rect."""
    return [e for e in items if any(Rect(*p).intersect(rect) for p in e["visible"])]


def by_name(items, name):
    return [e for e in items if e["name"] == name]


# ---------------------------------------------------------------- scenarios

def scenario_occlusion():
    """systemsettings and dolphin overlap; whichever is on top must hide the
    other's targets in the overlap, partially covered ones keep only their
    visible part."""
    missing = [c for c in ('systemsettings', 'org.kde.dolphin') if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_move("systemsettings", 0, 0, 760, 700)
    ctl.kwin_move("org.kde.dolphin", 420, 80, 700, 600)
    ctl.kwin_raise("org.kde.dolphin")
    _, items = settle()
    screenshot("occlusion-dolphin-on-top")
    settings_id, dolphin_id = window_id("systemsettings"), window_id("org.kde.dolphin")
    dolphin = window_frame("org.kde.dolphin")
    expect(any(e["window"] == dolphin_id for e in items), "dolphin has no targets")
    leaked = [e for e in targets_in(items, dolphin) if e["window"] == settings_id]
    expect(not leaked, f"{len(leaked)} systemsettings targets visible under dolphin: "
                       f"{[e['name'] for e in leaked[:5]]}")
    partial = [e for e in items if e["window"] == settings_id
               and region_area([Rect(*p) for p in e["visible"]]) < Rect(*e["rect"]).area - 1]
    expect(partial, "expected at least one partially covered systemsettings target")

    ctl.kwin_raise("systemsettings")
    _, items = settle()
    screenshot("occlusion-settings-on-top")
    settings = window_frame("systemsettings")
    leaked = [e for e in targets_in(items, settings) if e["window"] == dolphin_id]
    expect(not leaked, f"{len(leaked)} dolphin targets visible under systemsettings: "
                       f"{[e['name'] for e in leaked[:5]]}")
    return f"{len(partial)} partially covered targets"


def scenario_move():
    """Moving a window shifts its targets exactly, keeps their ids."""
    missing = [c for c in ('systemsettings',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_raise("systemsettings")
    ctl.kwin_move("systemsettings", 0, 0, 760, 700)
    _, before = settle()
    ctl.kwin_move("systemsettings", 100, 20)
    _, after = settle()
    screenshot("move-after")
    settings_id = window_id("systemsettings")
    before_by_id = {e["id"]: e for e in before if e["window"] == settings_id}
    moved = [e for e in after if e["id"] in before_by_id]
    expect(len(moved) >= 0.8 * len(before_by_id),
           f"ids not stable across move: {len(moved)} of {len(before_by_id)} kept")
    wrong = [e for e in moved
             if abs(e["rect"][0] - before_by_id[e["id"]]["rect"][0] - 100) > 0.6
             or abs(e["rect"][1] - before_by_id[e["id"]]["rect"][1] - 20) > 0.6]
    expect(not wrong, f"{len(wrong)} targets did not move by (100,20): "
                      f"{[(e['name'], e['rect'][:2]) for e in wrong[:4]]}")
    return f"{len(moved)} targets moved exactly"


def _sidebar_scroll_bar():
    Atspi = ctl._atspi()
    bars = [n for n in ctl.find("scroll bar", "", limit=50)
            if n.get_extents(Atspi.CoordType.WINDOW).width > 0]
    # QML scroll bars report a 0..0 Value range, so scroll via "Increase".
    return min(bars, key=lambda n: n.get_extents(Atspi.CoordType.WINDOW).x)


def scenario_scroll():
    """Scrolling the System Settings sidebar (QML ListView). Far scrolls
    recycle delegates (children-changed events); a small scroll moves rows
    without any AT-SPI event — published positions must follow anyway."""
    missing = [c for c in ('systemsettings',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_raise("systemsettings")
    ctl.kwin_move("systemsettings", 0, 0, 760, 700)
    _, items = settle()
    expect(not by_name(items, "About this System"), "'About this System' visible before scrolling")
    bar = _sidebar_scroll_bar()
    for _ in range(30):
        bar.do_action(0)
    _, items = settle(timeout_s=6)
    screenshot("scroll-far")
    expect(by_name(items, "About this System"), "'About this System' not a target after scrolling far")
    stale = ctl.stale_targets(items)
    expect(not stale, f"after far scroll, {len(stale)} stale: {[e['name'] for e in stale[:5]]}")

    bar.do_action(1)  # one small step back
    _, items = settle(timeout_s=6)
    screenshot("scroll-small")
    stale = ctl.stale_targets(items)
    expect(not stale, f"after small scroll, {len(stale)} stale: {[e['name'] for e in stale[:5]]}")
    return "ok"


def eis(*args):
    """Real input into the test desktop (tools/testdesk/eis_input.py, host)."""
    subprocess.run([*HOST_RUN, "python3",
                    os.path.join(REPO, "tools/testdesk/eis_input.py"), *map(str, args)],
                   check=True, stdout=subprocess.DEVNULL, timeout=30)


def scenario_chromium():
    """Chromium Flatpak with a throwaway profile (tools/testdesk/chromium.sh,
    page.html). Its own UI (tab strip, toolbar) reports logical pixels, the
    page DIP; the tab is a target, a closed <select> one target, the page
    itself none next to its controls. Chromium sends no AT-SPI event when
    the page or a list in it scrolls (test desktop 2026-11-12): only the
    active-window poll can notice, so targets must follow within seconds."""
    if not has_window("org.chromium.Chromium"):
        return "skipped (not running: tools/testdesk/chromium.sh)"
    ctl.kwin_raise("org.chromium.Chromium")
    ctl.kwin_move("org.chromium.Chromium", 60, 30, 1000, 660)
    eis("scroll", 900, 500, -30)  # page to the top
    settle(timeout_s=8)
    eis("scroll", 200, 330, -10)  # list to the top
    _, items = settle(timeout_s=8)
    screenshot("chromium-top")
    for name in ("Reload", "Address and search bar", "Erster Knopf", "Listeneintrag 1"):
        expect(by_name(items, name), f"{name!r} not a target")
    expect(any(e["role"] == "page tab" for e in items), "the browser tab is not a target")
    expect(not [e for e in items if e["role"] == "menu item"], "option inside the closed <select> is a target")
    expect(not [e for e in items if e["role"] == "document web"], "the page itself is a target")
    stale = ctl.stale_targets(items)
    expect(not stale, f"{len(stale)} stale: {[e['name'] for e in stale[:5]]}")

    x, y = by_name(items, "Erster Knopf")[0]["target"]
    t0 = time.monotonic()
    eis("scroll", x + 400, y, 30)  # beside the list: scrolls the page to the bottom
    scrolled = lambda it: by_name(it, "Knopf ganz unten") and not by_name(it, "Erster Knopf")
    items = wait_for(scrolled, 8)
    page_s = time.monotonic() - t0
    screenshot("chromium-page-scrolled")
    expect(scrolled(items), "page scroll not picked up within 8 s")
    stale = ctl.stale_targets(items)
    expect(not stale, f"after page scroll, {len(stale)} stale: {[e['name'] for e in stale[:5]]}")
    eis("scroll", x + 400, y, -30)
    items = wait_for(lambda it: by_name(it, "Erster Knopf"), 8)
    expect(by_name(items, "Erster Knopf"), "scrolling back not picked up within 8 s")

    x, y = by_name(items, "Listeneintrag 1")[0]["target"]
    t0 = time.monotonic()
    eis("scroll", x, y, 3)
    scrolled = lambda it: by_name(it, "Listeneintrag 8") and not by_name(it, "Listeneintrag 1")
    items = wait_for(scrolled, 8)
    list_s = time.monotonic() - t0
    screenshot("chromium-list-scrolled")
    expect(scrolled(items), "list scroll inside the page not picked up within 8 s")
    stale = ctl.stale_targets(items)
    expect(not stale, f"after list scroll, {len(stale)} stale: {[e['name'] for e in stale[:5]]}")
    eis("scroll", x, y, -3)
    return f"ok, page scroll picked up after {page_s:.1f}s, list scroll after {list_s:.1f}s"


def scenario_menu():
    """Right-click a file in Dolphin: the context menu (an xdg_popup, its
    own KWin window above everything) must become targets, and nothing
    under the menu may stay a target. Escape closes it again: its items
    must disappear."""
    missing = [c for c in ('org.kde.dolphin',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_raise("org.kde.dolphin")
    ctl.kwin_move("org.kde.dolphin", 300, 60, 800, 600)
    _, before = settle()
    dolphin_id = window_id("org.kde.dolphin")
    files = [e for e in before if e["window"] == dolphin_id and e["role"] == "list item"
             and Rect(*e["rect"]).x > 450]
    expect(files, "no file icons in dolphin to right-click")
    x, y = files[0]["target"]
    eis("rightclick", x, y)
    _, items = settle(timeout_s=6)
    screenshot("menu-open")
    before_ids = {b["id"] for b in before}
    new = [e for e in items if e["id"] not in before_ids]
    menu_items = [e for e in new if "menu item" in e["role"]]
    popups = [w for w in ctl.kwin_eval(ctl._WINDOWS_JS) if w["popup"]]
    expect(popups, "no popup window appeared in KWin after right-click")
    expect(menu_items, f"no menu items became targets (KWin popups: {[p['frame'] for p in popups]}, "
                       f"new targets: {[(e['role'], e['name']) for e in new[:6]]})")
    names = {e["name"] for e in menu_items}
    missing = {"Cut", "Copy", "Rename…", "Properties", "Move to Trash"} - names
    expect(not missing, f"context menu items missing as targets: {sorted(missing)}")
    inside = [e for e in menu_items if not Rect(*e["rect"]).intersect(Rect(*popups[-1]["frame"]))]
    expect(not inside, f"menu items outside the popup: {[e['name'] for e in inside]}")
    popup = Rect(*popups[-1]["frame"])
    under = [e for e in targets_in(items, popup) if e["window"] != popups[-1]["id"]
             and "menu item" not in e["role"]]
    expect(not under, f"{len(under)} targets under the open menu: {[e['name'] for e in under[:4]]}")
    eis("key", "esc")
    _, items = settle(timeout_s=6)
    left = [e for e in items if e["id"] in {m["id"] for m in menu_items}]
    expect(not left, f"{len(left)} menu items still targets after closing the menu")
    return f"{len(menu_items)} menu items"


def scenario_submenu():
    """Dolphin's hamburger menu and its submenu (Create New) open at once:
    both popups' items are targets, the submenu covers its parent's items
    where it overlaps; Escape twice closes both. (Dolphin hides its menu
    bar by default.)"""
    missing = [c for c in ('org.kde.dolphin',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_raise("org.kde.dolphin")
    ctl.kwin_move("org.kde.dolphin", 100, 60, 800, 600)
    _, before = settle()
    button = next((e for e in before if e["name"] == "Open Menu"), None)
    expect(button, "no 'Open Menu' target in dolphin")
    eis("click", *button["target"])
    _, items = settle(timeout_s=6)
    create = next((e for e in items if e["name"] == "Create New"), None)
    expect(create, f"'Create New' not a target after opening the menu "
                   f"(menu items: {[e['name'] for e in items if 'menu item' in e['role']][:8]})")
    eis("click", *create["target"])
    time.sleep(1.0)
    _, items = settle(timeout_s=6)
    screenshot("submenu-open")
    popups = [w for w in ctl.kwin_eval(ctl._WINDOWS_JS) if w["popup"]]
    expect(len(popups) >= 2, f"expected File menu + submenu popups, KWin has {len(popups)}")
    by_window = {}
    for e in items:
        if "menu item" in e["role"]:
            by_window.setdefault(e["window"], []).append(e["name"])
    popup_ids = [p["id"] for p in popups]
    expect(all(p in by_window for p in popup_ids),
           f"popups without menu-item targets: {[p for p in popup_ids if p not in by_window]}")
    failures = ctl.check(verbose=False)
    expect(not failures, f"with submenu open: {failures[:3]}")
    eis("key", "esc", "esc")
    _, items = settle(timeout_s=6)
    left = [e for e in items if e["window"] in popup_ids]
    expect(not left, f"{len(left)} targets of closed menus remain")
    return f"{len(popups)} popups, items per popup {[len(v) for v in by_window.values()]}"


def wait_for(predicate, timeout_s=6.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, items = ctl.elements()
        if predicate(items):
            return items
        time.sleep(0.2)
    return ctl.elements()[1]


def scenario_flyout():
    """Needs plasmashell. Clicking the panel clock opens the calendar
    flyout (a KWin appletPopup window in Plasma 6.6): its controls become
    targets within 1.5 s, nothing under it stays a target, Escape closes
    it and its targets go away."""
    if not has_window_class("plasmashell"):
        return "skipped (no plasmashell)"
    _, before = settle()
    clock = next((e for e in before if e["name"] in ("Digital Clock",) or e["name"].startswith("Today is")), None)
    expect(clock, "no clock target in the panel")
    before_windows = {w["id"] for w in ctl.kwin_eval(ctl._WINDOWS_JS)}
    t0 = time.monotonic()
    eis("click", *clock["target"])
    flyout = None
    deadline = time.monotonic() + 3
    while flyout is None and time.monotonic() < deadline:
        flyout = next((w for w in ctl.kwin_eval(ctl._WINDOWS_JS) if w["id"] not in before_windows), None)
    expect(flyout, "no new KWin window after clicking the clock")
    items = wait_for(lambda items: any(e["window"] == flyout["id"] for e in items), 1.5)
    latency = time.monotonic() - t0
    screenshot("flyout-open")
    mine = [e for e in items if e["window"] == flyout["id"]]
    expect(mine, f"flyout has no targets 1.5 s after opening")
    failures = ctl.check(verbose=False)
    expect(not failures, f"with flyout open: {failures[:3]}")
    eis("key", "esc")
    items = wait_for(lambda items: not any(e["window"] == flyout["id"] for e in items), 3)
    expect(not any(e["window"] == flyout["id"] for e in items), "flyout targets remain after Escape")
    return f"{len(mine)} flyout targets after {latency:.1f}s"


def scenario_desktop_icons():
    """Needs plasmashell. Desktop icons (Plasma FolderView canvases) are
    targets where the desktop is visible."""
    if not has_window_class("plasmashell"):
        return "skipped (no plasmashell)"
    for cls in ("systemsettings", "org.kde.dolphin"):
        if has_window(cls):
            ctl.kwin_move(cls, 400, 150, 700, 450)
    _, items = settle()
    icons = [e for e in items if e["role"] == "canvas" and e["name"]]
    expect(icons, "no desktop icon targets")
    return f"{len(icons)} icons: {[e['name'] for e in icons]}"


def scenario_desktops():
    """Window on another virtual desktop is not a target; switching shows it."""
    missing = [c for c in ('org.kde.dolphin',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    ctl.kwin_eval("(function(){ if (workspace.desktops.length < 2) "
                  "workspace.createDesktop(1, 'Second'); return workspace.desktops.length; })()")
    ctl.kwin_eval(ctl._window_js("org.kde.dolphin",
                                 "w.desktops = [workspace.desktops[1]]; return w.caption;"))
    _, items = settle()
    screenshot("desktops-first")
    expect(not any(e["window"] == window_id("org.kde.dolphin") for e in items),
           "dolphin (moved to desktop 2) still has targets on desktop 1")
    sticky = {w["id"] for w in ctl.kwin_eval(
        "(function(){var o=[];var s=workspace.stackingOrder;for(var i=0;i<s.length;i++)"
        "if(s[i].onAllDesktops)o.push({id:String(s[i].internalId)});return o;})()")}
    frames_on_first = {e["window"] for e in items} - sticky
    ctl.kwin_eval("(function(){ workspace.currentDesktop = workspace.desktops[1]; return 1; })()")
    _, items2 = settle()
    screenshot("desktops-second")
    expect(items2, "no targets on the second desktop")
    expect(not ({e["window"] for e in items2} & frames_on_first),
           "windows from desktop 1 still have targets on desktop 2")
    ctl.kwin_eval("(function(){ workspace.currentDesktop = workspace.desktops[0]; "
                  "var s = workspace.stackingOrder; for (var i = 0; i < s.length; i++) "
                  "if (String(s[i].resourceClass) === 'org.kde.dolphin') s[i].desktops = [workspace.desktops[0]];"
                  " return 1; })()")
    settle()
    return f"{len(items2)} targets on desktop 2"


def outputs():
    return ctl.kwin_eval("(function(){var o=[];var s=workspace.screens;for(var i=0;i<s.length;i++)"
                         "o.push({name:s[i].name,geo:[s[i].geometry.x,s[i].geometry.y,"
                         "s[i].geometry.width,s[i].geometry.height],scale:s[i].devicePixelRatio});"
                         "return o;})()")


def set_scale(output, scale):
    subprocess.run([*HOST_RUN,
                    "kscreen-doctor", f"output.{output}.scale.{scale}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    time.sleep(1.0)


def scenario_multimonitor():
    """Needs TESTDESK_OUTPUTS=2. Window on the second output, a window
    spanning both (parts in the gap between outputs dropped), then the
    second output at a different scale."""
    missing = [c for c in ('org.kde.dolphin',) if not has_window(c)]
    if missing:
        return f"skipped (not running: {missing})"
    outs = outputs()
    if len(outs) < 2:
        return "skipped (single output)"
    second = Rect(*outs[1]["geo"])
    dolphin_id = window_id("org.kde.dolphin")
    ctl.kwin_move("org.kde.dolphin", second.x + 100, 40, 700, 600)
    ctl.kwin_raise("org.kde.dolphin")
    _, items = settle()
    screenshot("multimon-second")
    mine = [e for e in items if e["window"] == dolphin_id]
    expect(mine, "dolphin on the second output has no targets")
    outside = [e for e in mine if any(not Rect(*p).intersect(second) for p in e["visible"])]
    expect(not outside, f"{len(outside)} dolphin targets not on the second output")
    failures = ctl.check(verbose=False)
    expect(not failures, f"on second output: {failures[:3]}")

    first = Rect(*outs[0]["geo"])
    ctl.kwin_move("org.kde.dolphin", first.x2 - 350, 40, 700, 600)
    _, items = settle()
    screenshot("multimon-spanning")
    mine = [e for e in items if e["window"] == dolphin_id]
    on = {i for i, o in enumerate(outs) for e in mine for p in e["visible"]
          if Rect(*p).intersect(Rect(*o["geo"]))}
    expect(on == {0, 1}, f"spanning window has targets on outputs {sorted(on)}, expected both")
    failures = ctl.check(verbose=False)
    expect(not failures, f"spanning: {failures[:3]}")

    set_scale(outs[1]["name"], "1")
    outs = outputs()
    second = Rect(*outs[1]["geo"])
    ctl.kwin_move("org.kde.dolphin", second.x + 100, 40, 700, 600)
    _, items = settle()
    screenshot("multimon-mixed-scale")
    failures = ctl.check(verbose=False)
    set_scale(outs[1]["name"], "1.1")
    expect(not failures, f"mixed scales ({outs[0]['scale']}/{outs[1]['scale']}): {failures[:3]}")
    return f"{len(mine)} targets spanning, scales {outs[0]['scale']}/{outs[1]['scale']}"


def scenario_coverage():
    """Every visible normal window has targets (i.e. was matched to its
    AT-SPI frame and read) — catches matching gaps such as Flatpak's proxy
    pid, and whole toolkits being filtered out (GTK4 without ENABLED)."""
    _, items = settle()
    with_targets = {e["window"] for e in items}
    windows = [w for w in ctl.kwin_eval(ctl._WINDOWS_JS)
               if w["normal"] and not w["minimized"] and w["cls"] != "python3.14"]
    # fully covered windows legitimately have none
    regions = {}
    from deckery_atspi.scene import scene_from_payload
    missing = []
    for w in windows:
        frame = Rect(*w["frame"])
        above = [Rect(*o["frame"]) for o in windows[windows.index(w) + 1:]]
        from deckery_atspi.geometry import region_subtract
        region = [frame]
        for a in above:
            region = region_subtract(region, a)
        if region and w["id"] not in with_targets:
            missing.append(f"{w['cls']} {w['caption'][:30]!r}")
    expect(not missing, f"visible windows without targets: {missing}")
    return f"{len(windows)} windows covered"


def scenario_invariants():
    failures = ctl.check(verbose=False)
    expect(not failures, "; ".join(failures[:5]))
    return "ok"


SCENARIOS = {
    "occlusion": scenario_occlusion,
    "move": scenario_move,
    "scroll": scenario_scroll,
    "menu": scenario_menu,
    "submenu": scenario_submenu,
    "flyout": scenario_flyout,
    "chromium": scenario_chromium,
    "desktop_icons": scenario_desktop_icons,
    "desktops": scenario_desktops,
    "multimonitor": scenario_multimonitor,
    "coverage": scenario_coverage,
    "invariants": scenario_invariants,
}


def main(names):
    names = names or list(SCENARIOS)
    failed = 0
    for name in names:
        t0 = time.monotonic()
        try:
            detail = SCENARIOS[name]()
            print(f"PASS {name:12} {time.monotonic() - t0:5.1f}s  {detail}", flush=True)
        except ScenarioFailed as e:
            failed += 1
            print(f"FAIL {name:12} {time.monotonic() - t0:5.1f}s  {e}", flush=True)
        except Exception:
            failed += 1
            print(f"ERROR {name:11} {time.monotonic() - t0:5.1f}s", flush=True)
            traceback.print_exc()
        try:
            failures = ctl.check(verbose=False)
            if failures:
                print(f"     invariants after {name}: {failures[:3]}")
        except Exception:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
