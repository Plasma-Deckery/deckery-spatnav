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
SHOTS = os.path.join(os.environ["XDG_RUNTIME_DIR"], "deckery-testdesk", "shots")


class ScenarioFailed(AssertionError):
    pass


def expect(condition, message):
    if not condition:
        raise ScenarioFailed(message)


def screenshot(name):
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, f"{name}.png")
    subprocess.run(["distrobox-host-exec", os.path.join(REPO, "tools/testdesk/run.sh"),
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
    subprocess.run(["distrobox-host-exec", os.path.join(REPO, "tools/testdesk/run.sh"), "python3",
                    os.path.join(REPO, "tools/testdesk/eis_input.py"), *map(str, args)],
                   check=True, stdout=subprocess.DEVNULL, timeout=30)


def scenario_menu():
    """Right-click a file in Dolphin: the context menu (an xdg_popup, its
    own KWin window above everything) must become targets, and nothing
    under the menu may stay a target. Escape closes it again: its items
    must disappear."""
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


def scenario_desktops():
    """Window on another virtual desktop is not a target; switching shows it."""
    ctl.kwin_eval("(function(){ if (workspace.desktops.length < 2) "
                  "workspace.createDesktop(1, 'Second'); return workspace.desktops.length; })()")
    ctl.kwin_eval(ctl._window_js("org.kde.dolphin",
                                 "w.desktops = [workspace.desktops[1]]; return w.caption;"))
    _, items = settle()
    screenshot("desktops-first")
    expect(not any(e["window"] == window_id("org.kde.dolphin") for e in items),
           "dolphin (moved to desktop 2) still has targets on desktop 1")
    frames_on_first = {e["window"] for e in items}
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


def scenario_invariants():
    failures = ctl.check(verbose=False)
    expect(not failures, "; ".join(failures[:5]))
    return "ok"


SCENARIOS = {
    "occlusion": scenario_occlusion,
    "move": scenario_move,
    "scroll": scenario_scroll,
    "menu": scenario_menu,
    "desktops": scenario_desktops,
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
