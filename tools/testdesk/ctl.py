"""Test-desktop remote control for the v2 test loop.

Run inside the test session in the deckery box:
    tools/testdesk/run_box.sh python3 tools/testdesk/ctl.py <command> ...

Commands:
  kwin '<js expression>'          evaluate JS in the nested KWin, print JSON result
  windows                         list KWin windows (stacking order, geometry)
  move <class> <x> <y> [w h]      move/resize the first window of resource class
  raise <class>                   activate (raise) the first window of class
  find <role> <name>              list matching AT-SPI objects (substring match)
  do <role> <name> [action]       run an AT-SPI action (default: first action)
  scroll <role> <name>            scroll the object into view (Component.scroll_to)
  value <role> <name> <v|max|min> set an AT-SPI Value (scroll bars, sliders)
  elements                        print the backend's published targets
  check                           run the invariant checks, exit 1 on failure
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gi.repository import Gio, GLib

BACKEND = "org.deckery.Atspi"


def bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION)


# ---------------------------------------------------------------- KWin

_RECEIVER_XML = """<node><interface name="org.deckery.TestCtl">
<method name="Result"><arg type="s" direction="in"/></method></interface></node>"""


def kwin_eval(expression, timeout_s=5):
    """Runs `expression` inside KWin and returns its JSON-decoded value. KWin
    scripts can't return values over /Scripting, so the script calls back
    into a throwaway D-Bus name owned by this process."""
    connection = bus()
    name = f"org.deckery.TestCtl.p{os.getpid()}"
    result = {}
    loop = GLib.MainLoop()

    def on_call(conn, sender, path, iface, method, params, invocation):
        result["value"] = params.unpack()[0]
        invocation.return_value(None)
        loop.quit()

    reg = connection.register_object("/Ctl", Gio.DBusNodeInfo.new_for_xml(_RECEIVER_XML).interfaces[0],
                                     on_call, None, None)
    owner = Gio.bus_own_name_on_connection(connection, name, Gio.BusNameOwnerFlags.NONE, None, None)
    script = (f"var __r; try {{ __r = JSON.stringify({{ok: true, v: ({expression})}}); }}"
              f" catch (e) {{ __r = JSON.stringify({{ok: false, v: String(e)}}); }}\n"
              f'callDBus("{name}", "/Ctl", "org.deckery.TestCtl", "Result", __r);\n')
    with tempfile.NamedTemporaryFile("w", suffix=".js", dir=os.environ.get("XDG_RUNTIME_DIR"),
                                     delete=False) as f:
        f.write(script)
    plugin = f"deckery-testctl-{os.getpid()}"
    try:
        (sid,) = connection.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                                      GLib.Variant("(ss)", (f.name, plugin)), GLib.VariantType("(i)"),
                                      0, 2000, None).unpack()
        connection.call_sync("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run",
                             None, None, 0, 2000, None)
        GLib.timeout_add(int(timeout_s * 1000), loop.quit)
        loop.run()
    finally:
        connection.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                             GLib.Variant("(s)", (plugin,)), None, 0, 2000, None)
        os.unlink(f.name)
        Gio.bus_unown_name(owner)
        connection.unregister_object(reg)
    if "value" not in result:
        raise RuntimeError("KWin script did not answer")
    answer = json.loads(result["value"])
    if not answer["ok"]:
        raise RuntimeError(f"KWin script error: {answer['v']}")
    return answer["v"]


_WINDOWS_JS = """(function () {
    var out = [];
    var s = workspace.stackingOrder;
    for (var i = 0; i < s.length; i++) {
        var w = s[i];
        out.push({id: String(w.internalId), cls: String(w.resourceClass), caption: w.caption, pid: w.pid,
                  frame: [w.frameGeometry.x, w.frameGeometry.y, w.frameGeometry.width, w.frameGeometry.height],
                  minimized: w.minimized, popup: w.popupWindow, normal: w.normalWindow});
    }
    return out;
})()"""


def _window_js(cls, body):
    return f"""(function () {{
        var s = workspace.stackingOrder;
        for (var i = s.length - 1; i >= 0; i--) {{
            var w = s[i];
            if (String(w.resourceClass) === {json.dumps(cls)} && w.normalWindow) {{ {body} }}
        }}
        throw "no normal window of class " + {json.dumps(cls)};
    }})()"""


def kwin_qml(body):
    """Runs JS `body` in a declarative (QML) KWin script, for what plain
    scripts can't do: assigning frameGeometry needs a real QRectF
    (Qt.rect), and plain scripts silently ignore a JS object or a mutated
    copy. `Workspace` is the workspace singleton there. No return value."""
    connection = bus()
    plugin = f"deckery-testctl-qml-{os.getpid()}"
    source = ("import QtQuick\nimport org.kde.kwin\n"
              "Item { Component.onCompleted: { " + body + " } }\n")
    with tempfile.NamedTemporaryFile("w", suffix=".qml", dir=os.environ.get("XDG_RUNTIME_DIR"),
                                     delete=False) as f:
        f.write(source)
    try:
        (sid,) = connection.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                                      "loadDeclarativeScript", GLib.Variant("(ss)", (f.name, plugin)),
                                      GLib.VariantType("(i)"), 0, 2000, None).unpack()
        connection.call_sync("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run",
                             None, None, 0, 2000, None)
        time.sleep(0.3)
    finally:
        connection.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                             GLib.Variant("(s)", (plugin,)), None, 0, 2000, None)
        os.unlink(f.name)


def kwin_move(cls, x, y, w=None, h=None):
    size = f"{w}, {h}" if w else "c.frameGeometry.width, c.frameGeometry.height"
    kwin_qml(f"""var s = Workspace.stackingOrder;
        for (var i = s.length - 1; i >= 0; i--) {{
            var c = s[i];
            if (String(c.resourceClass) === {json.dumps(cls)} && c.normalWindow) {{
                c.frameGeometry = Qt.rect({x}, {y}, {size}); break;
            }}
        }}""")
    return kwin_eval(_window_js(cls, "return [w.frameGeometry.x, w.frameGeometry.y, "
                                     "w.frameGeometry.width, w.frameGeometry.height];"))


def kwin_raise(cls):
    return kwin_eval(_window_js(cls, "workspace.activeWindow = w; return w.caption;"))


# ---------------------------------------------------------------- AT-SPI

def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    return Atspi


def find(role, name, limit=20):
    Atspi = _atspi()
    out = []

    def walk(node, depth):
        if len(out) >= limit or depth > 40:
            return
        try:
            if node.get_role_name() == role and name.lower() in (node.get_name() or "").lower():
                out.append(node)
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    walk(child, depth + 1)
        except Exception:
            pass

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        walk(desktop.get_child_at_index(i), 0)
    return out


def describe(node):
    Atspi = _atspi()
    e = node.get_extents(Atspi.CoordType.WINDOW)
    actions = []
    try:
        for i in range(node.get_n_actions()):
            actions.append(node.get_action_name(i))
    except Exception:
        pass
    return f"[{node.get_role_name()}] {node.get_name()!r} win=({e.x},{e.y},{e.width},{e.height}) actions={actions}"


def do_action(role, name, action=None):
    nodes = find(role, name, limit=1)
    if not nodes:
        raise RuntimeError(f"no [{role}] matching {name!r}")
    node = nodes[0]
    index = 0
    if action:
        names = [node.get_action_name(i) for i in range(node.get_n_actions())]
        index = names.index(action)
    node.do_action(index)
    return describe(node)


def scroll_into_view(role, name):
    Atspi = _atspi()
    nodes = find(role, name, limit=1)
    if not nodes:
        raise RuntimeError(f"no [{role}] matching {name!r}")
    nodes[0].scroll_to(Atspi.ScrollType.ANYWHERE)
    return describe(nodes[0])


def set_value(role, name, value):
    """Set an AT-SPI Value (e.g. a scroll bar); value may be 'max'/'min'."""
    Atspi = _atspi()
    nodes = [n for n in find(role, name, limit=50)
             if n.get_maximum_value() > n.get_minimum_value()
             and n.get_extents(Atspi.CoordType.WINDOW).width > 0]
    if not nodes:
        raise RuntimeError(f"no usable [{role}] matching {name!r}")
    node = nodes[0]
    if value == "max":
        value = node.get_maximum_value()
    elif value == "min":
        value = node.get_minimum_value()
    node.set_current_value(float(value))
    return f"{describe(node)} value={node.get_current_value()} of {node.get_maximum_value()}"


# ---------------------------------------------------------------- backend

def elements():
    version, text = bus().call_sync(BACKEND, "/Elements", "org.deckery.Atspi.Elements", "Get", None,
                                    GLib.VariantType("(us)"), 0, 3000, None).unpack()
    return version, json.loads(text)


def wait_for_version(after, timeout_s=5.0):
    """Waits until the backend has published something newer than `after`
    (or the timeout passes), then returns the latest snapshot."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        version, items = elements()
        if version > after:
            # let a burst of follow-up updates settle
            time.sleep(0.4)
            return elements()
        time.sleep(0.1)
    return elements()


def live_rects():
    """{element id: window-relative rect} for every object in the AT-SPI
    tree, queried live (no caches) — ground truth for freshness checks."""
    from deckery_atspi.atspi_frames import element_id
    Atspi = _atspi()
    out = {}

    def walk(node, depth):
        if depth > 40:
            return
        try:
            e = node.get_extents(Atspi.CoordType.WINDOW)
            out[element_id(node)] = (e.x, e.y, e.width, e.height)
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    walk(child, depth + 1)
        except Exception:
            pass

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        walk(desktop.get_child_at_index(i), 0)
    return out


def stale_targets(items, tolerance=1.0):
    """Published targets whose position disagrees with AT-SPI right now.
    Within one window every target is offset from its live window-relative
    rect by the same window origin; the majority offset is taken as truth
    (no Chromium windows here, so no DIP scaling to account for)."""
    from collections import Counter
    live = live_rects()
    by_window = {}
    gone = []
    for e in items:
        if e.get("inferred"):
            continue  # menu_repair geometry: AT-SPI's own rect is known-wrong
        r = live.get(e["id"])
        if r is None:
            gone.append(e)
            continue
        delta = (round(e["rect"][0] - r[0]), round(e["rect"][1] - r[1]))
        size_ok = abs(e["rect"][2] - r[2]) <= tolerance and abs(e["rect"][3] - r[3]) <= tolerance
        by_window.setdefault(e["window"], []).append((e, delta, size_ok))
    stale = list(gone)
    for entries in by_window.values():
        origin, _ = Counter(d for _, d, _ in entries).most_common(1)[0]
        stale += [e for e, d, ok in entries
                  if not ok or abs(d[0] - origin[0]) > tolerance or abs(d[1] - origin[1]) > tolerance]
    return stale


def check(verbose=True):
    """Invariants that must hold for any scene. Returns list of failures."""
    from deckery_atspi.geometry import Rect
    windows = kwin_eval(_WINDOWS_JS)
    screens = kwin_eval("(function(){var o=[];var s=workspace.screens;for(var i=0;i<s.length;i++)"
                        "o.push([s[i].geometry.x,s[i].geometry.y,s[i].geometry.width,s[i].geometry.height]);"
                        "return o;})()")
    _, items = elements()
    failures = []
    ids = [e["id"] for e in items]
    if len(ids) != len(set(ids)):
        failures.append(f"duplicate ids: {len(ids) - len(set(ids))}")
    screen_rects = [Rect(*s) for s in screens]
    for e in items:
        for part in e["visible"]:
            r = Rect(*part)
            if not any(r.intersect(s) for s in screen_rects):
                failures.append(f"off-screen part: {e['role']} {e['name']!r} {part}")
        tx, ty = e["target"]
        if not any(part[0] - 0.01 <= tx <= part[0] + part[2] + 0.01 and
                   part[1] - 0.01 <= ty <= part[1] + part[3] + 0.01 for part in e["visible"]):
            failures.append(f"target outside visible parts: {e['role']} {e['name']!r}")
    # Occlusion: a visible part must lie inside its own window and must not
    # overlap any window stacked above it (KWin's stacking order is truth).
    order = {w["id"]: i for i, w in enumerate(windows)}
    mapped = [w for w in windows if not w["minimized"]]
    for e in items:
        own = next((w for w in mapped if w["id"] == e["window"]), None)
        if own is None:
            failures.append(f"target of unknown/minimized window: {e['role']} {e['name']!r}")
            continue
        above = [Rect(*w["frame"]) for w in mapped
                 if order[w["id"]] > order[own["id"]] and w["cls"] != "python3.14"]
        for part in e["visible"]:
            r = Rect(*part)
            inside = r.intersect(Rect(*own["frame"]))
            if inside is None or inside.area < r.area - 1:
                failures.append(f"part outside own window: {e['role']} {e['name']!r} {part}")
            for a in above:
                cut = r.intersect(a)
                if cut is not None and cut.area > 1:
                    failures.append(f"part under higher window: {e['role']} {e['name']!r} {part}")
    for e in stale_targets(items):
        failures.append(f"stale: {e['role']} {e['name']!r} published at {[round(c) for c in e['rect']]}")
    if verbose:
        print(f"{len(items)} targets, {len(windows)} windows, {len(failures)} failures")
        for f in failures:
            print("  FAIL", f)
    return failures


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "kwin":
        print(json.dumps(kwin_eval(args[0]), indent=1))
    elif cmd == "windows":
        for w in kwin_eval(_WINDOWS_JS):
            print(w)
    elif cmd == "move":
        print(kwin_move(args[0], *args[1:]))
    elif cmd == "raise":
        print(kwin_raise(args[0]))
    elif cmd == "find":
        for node in find(args[0], args[1]):
            print(describe(node))
    elif cmd == "do":
        print(do_action(*args))
    elif cmd == "value":
        print(set_value(*args))
    elif cmd == "scroll":
        print(scroll_into_view(*args))
    elif cmd == "elements":
        version, items = elements()
        print(f"version {version}, {len(items)} targets")
        for e in items:
            r = e["rect"]
            print(f"  {e['role']:14} {e['name'][:30]:30} rect=({r[0]:.0f},{r[1]:.0f},{r[2]:.0f},{r[3]:.0f}) "
                  f"parts={len(e['visible'])}")
    elif cmd == "check":
        return 1 if check() else 0
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
