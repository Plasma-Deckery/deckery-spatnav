"""Mode probe: how soon after real input does each signal arrive that a
control application could use to switch into a special mode (menu/popup
or modal dialog open, text field focused, slider/spin button/drop-down
selected)? Measures only — what to build on it is not decided
(BACKEND_V2_DESIGN.md §12).

Sources, each timestamped on arrival with time.monotonic() (the same
CLOCK_MONOTONIC eis_input.py stamps its input with on the host):
- kwin:    windowAdded/windowRemoved/windowActivated from a KWin script
           (popups are their own KWin windows), with popup/dialog/modal
           flags;
- atspi:   object:state-changed:focused (gained), :expanded, :showing of
           popup-like roles, window:activate/create,
           object:active-descendant-changed — each source's role, name,
           states and interfaces (Value: min/max/current) are asked right
           after arrival, the way a consumer would;
- backend: org.deckery.Atspi Changed (the slow full target list), and
           the first one listing a target in the new KWin window;
- role:    the role of the clicked target in the backend's list (known
           before the input; what a control app gets for free).

Run inside the test desktop, with the backend and the app running:
    tools/testdesk/run_box.sh python3 tools/testdesk/mode_probe.py gtk
    ... dolphin | systemsettings | chromium | firefox  [--json FILE]

Browsers need tools/testdesk/controls.html (CHROMIUM_PAGE=controls.html
tools/testdesk/chromium.sh, FIREFOX_PAGE=controls.html …/firefox.sh)."""

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib

import ctl

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTANCE = os.environ.get("DECKERY_TESTDESK", "")
HOST_RUN = ["distrobox-host-exec", "env", f"DECKERY_TESTDESK={INSTANCE}",
            os.path.join(REPO, "tools/testdesk/run.sh")]

# How long signals are collected after each input.
STEP_S = 2.5
POPUP_LIKE_ROLES = frozenset(("menu", "popup menu", "window", "dialog", "alert", "list", "list box",
                              "tool tip", "panel"))
STATES_OF_INTEREST = ("focused", "editable", "expanded", "expandable", "has-popup", "multi-line",
                      "single-line", "modal", "checkable", "checked", "read-only", "selectable",
                      "focusable", "active")

# ---------------------------------------------------------------- steps
#
# (label, action, selector, what it should switch into). action: "click",
# "rightclick", "key KEY...", "combo a+b". selector: ("name", text) or
# ("role", role) on the backend's targets of the app's windows; None for
# keys.

GTK_STEPS = [
    ("Textfeld", "click", ("name", "Textfeld"), "text"),
    ("Passwortfeld", "click", ("name", "Passwortfeld"), "text"),
    ("Mehrzeiliger Text", "click", ("name", "Mehrzeiliger Text"), "text"),
    ("Schieberegler", "click", ("name", "Schieberegler"), "slider"),
    ("Zahlenfeld", "click", ("name", "Zahlenfeld"), "spin"),
    ("Aktiver Knopf", "click", ("name", "Aktiver Knopf"), "-"),
    ("Tab", "key tab", None, "-"),
    ("Auswahlliste auf", "click", ("role", "combo box"), "popup"),
    ("Auswahlliste zu (Esc)", "key esc", None, "-"),
    ("Menü auf", "click", ("name", "Menü öffnen"), "popup"),
    ("Menü zu (Esc)", "key esc", None, "-"),
    ("Popover auf", "click", ("name", "Popover öffnen"), "popup"),
    ("Popover zu (Esc)", "key esc", None, "-"),
    ("Dialog auf", "click", ("name", "Dialog öffnen"), "dialog"),
    ("Dialog zu", "click", ("name", "Abbrechen"), "-"),
]
BROWSER_STEPS = [
    ("Textfeld", "click", ("name", "Textfeld"), "text"),
    ("Passwortfeld", "click", ("name", "Passwortfeld"), "text"),
    ("Mehrzeiliger Text", "click", ("name", "Mehrzeiliger Text"), "text"),
    ("Schieberegler", "click", ("name", "Schieberegler"), "slider"),
    ("Zahlenfeld", "click", ("name", "Zahlenfeld"), "spin"),
    ("Aktiver Knopf", "click", ("name", "Aktiver Knopf"), "-"),
    ("Tab", "key tab", None, "-"),
    ("Auswahlliste auf", "click", ("name", "Auswahlliste"), "popup"),
    # (Esc did not close Chromium's list on the test desktop)
    ("Auswahlliste zu (Option)", "click", ("role", "menu item"), "-"),
    ("Dialog auf", "click", ("name", "Dialog öffnen"), "dialog"),
    ("Dialog zu", "click", ("name", "Abbrechen"), "-"),
]
DOLPHIN_STEPS = [
    # (the first file already has the focus: no event)
    ("Datei", "click", ("role", "list item", 2), "-"),
    ("Adresszeile (Strg+L)", "combo ctrl+l", None, "text"),
    ("Adresszeile zu (Esc)", "key esc", None, "-"),
    ("Suche (Strg+F)", "combo ctrl+f", None, "text"),
    ("Suche zu (Esc)", "key esc", None, "-"),
    ("Kontextmenü auf", "rightclick", ("role", "list item"), "popup"),
    ("Kontextmenü zu (Esc)", "key esc", None, "-"),
    ("Menü auf", "click", ("name", "Open Menu"), "popup"),
    ("Menü zu (Esc)", "key esc", None, "-"),
    ("Neuer Ordner (Strg+Umschalt+N)", "combo ctrl+shift+n", None, "dialog"),
    ("Dialog zu (Esc)", "key esc", None, "-"),
]
# On the "Display & Monitor" page (Quick Settings has no drop-down or spin
# button; the search field is focused from the start).
SETTINGS_STEPS = [
    ("Knopf", "click", ("role", "radio button"), "-"),
    ("Suchfeld (Strg+F)", "combo ctrl+f", None, "text"),
    ("Schieberegler", "click", ("role", "slider"), "slider"),
    ("Zahlenfeld", "click", ("role", "spin button"), "spin"),
    ("Menüknopf auf", "click", ("role", "push button menu"), "popup"),
    ("Menü zu (Esc)", "key esc", None, "-"),
]
APPS = {
    "gtk": ("org.deckery.TestApp", GTK_STEPS),
    "chromium": ("org.chromium.Chromium", BROWSER_STEPS),
    "firefox": ("org.mozilla.firefox", BROWSER_STEPS),
    "dolphin": ("org.kde.dolphin", DOLPHIN_STEPS),
    "systemsettings": ("systemsettings", SETTINGS_STEPS),
}

# ---------------------------------------------------------------- sources

_PROBE_XML = """<node><interface name="org.deckery.ModeProbe">
<method name="Event"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>"""

_KWIN_JS = """
function info(w) {
    var g = w.frameGeometry;
    return JSON.stringify({id: String(w.internalId), cls: String(w.resourceClass), caption: String(w.caption),
        popup: !!w.popupWindow, normal: !!w.normalWindow, dialog: !!w.dialog, modal: !!w.modal,
        transient: !!w.transient, menu: !!(w.popupMenu || w.dropdownMenu), combo: !!w.comboBox,
        frame: [g.x, g.y, g.width, g.height]});
}
function send(kind, w) {
    callDBus("%(name)s", "/Probe", "org.deckery.ModeProbe", "Event", kind, w ? info(w) : "{}");
}
workspace.windowAdded.connect(function (w) { send("added", w); });
workspace.windowRemoved.connect(function (w) { send("removed", w); });
workspace.windowActivated.connect(function (w) { send("activated", w); });
"""


_STATE = {s: getattr(Atspi.StateType, s.upper().replace("-", "_")) for s in STATES_OF_INTEREST}


class Recorder:
    def __init__(self):
        self.events = []   # (t, source, kind, detail)

    def add(self, t, source, kind, detail):
        self.events.append((t, source, kind, detail))


class KWinSource:
    """A KWin script calling back on window changes, for the probe's lifetime."""

    def __init__(self, recorder):
        self._rec = recorder
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._name = f"org.deckery.ModeProbe.p{os.getpid()}"
        self._reg = self._bus.register_object(
            "/Probe", Gio.DBusNodeInfo.new_for_xml(_PROBE_XML).interfaces[0], self._on_call, None, None)
        self._owner = Gio.bus_own_name_on_connection(self._bus, self._name, Gio.BusNameOwnerFlags.NONE,
                                                     None, None)
        with tempfile.NamedTemporaryFile("w", suffix=".js", dir=os.environ.get("XDG_RUNTIME_DIR"),
                                         delete=False) as f:
            f.write(_KWIN_JS % {"name": self._name})
        self._file = f.name
        self._plugin = f"deckery-modeprobe-{os.getpid()}"
        (sid,) = self._bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                                     GLib.Variant("(ss)", (self._file, self._plugin)), GLib.VariantType("(i)"),
                                     0, 2000, None).unpack()
        self._bus.call_sync("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run",
                            None, None, 0, 2000, None)

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        t = time.monotonic()
        kind, text = params.unpack()
        self._rec.add(t, "kwin", kind, json.loads(text))
        invocation.return_value(None)

    def close(self):
        self._bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                            GLib.Variant("(s)", (self._plugin,)), None, 0, 2000, None)
        os.unlink(self._file)
        Gio.bus_unown_name(self._owner)
        self._bus.unregister_object(self._reg)


def describe(acc):
    """role, name, states, Value — the calls a consumer would make."""
    d = {}
    try:
        d["role"] = acc.get_role_name()
        d["name"] = acc.get_name() or ""
        states = acc.get_state_set()
        d["states"] = [s for s in STATES_OF_INTEREST if states.contains(_STATE[s])]
        ifaces = acc.get_interfaces()
        d["ifaces"] = [i for i in ifaces if i in ("Value", "EditableText", "Text", "Selection")]
        if "Value" in ifaces:
            d["value"] = [acc.get_minimum_value(), acc.get_current_value(), acc.get_maximum_value()]
        app = acc.get_application()
        d["app"] = app.get_name() if app else ""
    except Exception as e:
        d["error"] = str(e)[:80]
    return d


class AtspiSource:
    EVENTS = ("object:state-changed:focused", "object:state-changed:expanded",
              "object:state-changed:showing", "window:activate", "window:create",
              "object:active-descendant-changed")

    def __init__(self, recorder):
        self._rec = recorder
        self._listener = Atspi.EventListener.new(self._on_event)
        for e in self.EVENTS:
            self._listener.register(e)

    def _on_event(self, event):
        t = time.monotonic()
        kind = event.type
        if kind == "object:state-changed:focused" and not event.detail1:
            return
        try:
            src = event.source
            if kind == "object:state-changed:showing" and src.get_role_name() not in POPUP_LIKE_ROLES:
                return
            detail = describe(src)
            if kind == "object:active-descendant-changed":
                child = event.any_data
                if isinstance(child, Atspi.Accessible):
                    detail["descendant"] = describe(child)
        except Exception as e:
            detail = {"error": str(e)[:80]}
        detail["d1"] = event.detail1
        detail["t_described"] = time.monotonic() - t
        self._rec.add(t, "atspi", kind, detail)


class BackendSource:
    def __init__(self, recorder):
        self._rec = recorder
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._sub = self._bus.signal_subscribe(ctl.BACKEND, "org.deckery.Atspi.Elements", "Changed",
                                               "/Elements", None, Gio.DBusSignalFlags.NONE, self._on_signal)

    def _on_signal(self, conn, sender, path, iface, signal, params):
        t = time.monotonic()
        version, text = params.unpack()
        self._rec.add(t, "backend", "changed", {"version": version, "json": text})


# ---------------------------------------------------------------- run

def _select(items, windows, selector):
    """("name", text) or ("role", role[, index])."""
    kind, value, *index = selector
    mine = [e for e in items if e["window"] in windows]
    if kind == "name":
        found = [e for e in mine if e["name"] == value] or [e for e in mine if value in e["name"]]
    else:
        found = [e for e in mine if e["role"] == value]
    index = index[0] if index else 0
    return found[index] if len(found) > index else None


def _eis_args(action, element):
    if action in ("click", "rightclick"):
        return [action, *map(str, element["target"])]
    verb, *rest = action.split()
    return [verb, *rest]


def run_step(loop_ctx, recorder, cls, step):
    label, action, selector, mode = step
    windows = {w["id"] for w in ctl.kwin_eval(ctl._WINDOWS_JS) if w["cls"] == cls or w["popup"]
               or w["cls"] in ("gtk_testapp.py",)}
    element = None
    if selector:
        _, items = ctl.elements()
        element = _select(items, windows, selector)
        if element is None:
            return {"label": label, "mode": mode, "error": f"no target {selector}"}
    start = len(recorder.events)
    proc = subprocess.Popen([*HOST_RUN, "python3", os.path.join(REPO, "tools/testdesk/eis_input.py"),
                             *_eis_args(action, element)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    deadline = None
    while True:
        while loop_ctx.pending():
            loop_ctx.iteration(False)
        if deadline is None and proc.poll() is not None:
            deadline = time.monotonic() + STEP_S
        if deadline is not None and time.monotonic() > deadline:
            break
        loop_ctx.iteration(False) or time.sleep(0.002)
    out = proc.stdout.read()
    stamp = next((line.split() for line in out.splitlines() if line.startswith("input ")), None)
    if stamp is None:
        return {"label": label, "mode": mode, "error": "eis_input printed no input time"}
    press, release = float(stamp[1]), float(stamp[2])
    return {"label": label, "mode": mode, "press": press, "release": release,
            "role": element["role"] if element else None, "name": element["name"] if element else None,
            "events": recorder.events[start:]}


def _ms(t, t0):
    return f"{1000 * (t - t0):5.0f}"


def summarize(result):
    """One dict per step: first arrival of each signal, in ms after the
    press, and what it said."""
    if "error" in result:
        return {"label": result["label"], "error": result["error"]}
    t0 = result["press"]
    events = [e for e in result["events"] if e[0] >= t0 - 0.05]
    s = {"label": result["label"], "mode": result["mode"], "role": result["role"],
         "release_ms": round(1000 * (result["release"] - t0))}
    new_windows = set()
    for t, source, kind, d in events:
        if source == "kwin":
            key = f"kwin_{kind}"
            if key not in s:
                s[key] = (round(1000 * (t - t0)), {k: d.get(k) for k in ("cls", "popup", "dialog", "modal",
                                                                          "transient", "menu", "normal")})
            if kind == "added":
                new_windows.add(d.get("id"))
        elif source == "atspi":
            key = {"object:state-changed:focused": "focus", "object:state-changed:expanded": "expanded",
                   "object:state-changed:showing": "showing", "window:activate": "win_activate",
                   "window:create": "win_create",
                   "object:active-descendant-changed": "active_desc"}[kind]
            if key not in s:
                s[key] = (round(1000 * (t - t0)), {k: d.get(k) for k in ("role", "name", "states", "ifaces",
                                                                          "value", "d1", "descendant")
                                                   if d.get(k) not in (None, [], "")})
                s[key][1]["describe_ms"] = round(1000 * d.get("t_described", 0), 1)
        elif source == "backend":
            if "backend" not in s:
                s["backend"] = round(1000 * (t - t0))
            if new_windows and "backend_new_window" not in s:
                items = json.loads(d["json"])
                if any(e["window"] in new_windows for e in items):
                    s["backend_new_window"] = round(1000 * (t - t0))
    focus = [(t, d) for t, source, kind, d in events if kind == "object:state-changed:focused"]
    s["focus_count"] = len(focus)
    if len(focus) > 1:
        t, d = focus[-1]
        s["focus_last"] = (round(1000 * (t - t0)), {k: d.get(k) for k in ("role", "name", "states", "ifaces", "value")
                                                     if d.get(k) not in (None, [], "")})
    return s


def main(argv):
    if not argv or argv[0] not in APPS:
        print(__doc__)
        return 2
    app = argv[0]
    json_out = argv[argv.index("--json") + 1] if "--json" in argv else None
    cls, steps = APPS[app]
    if not any(w["cls"] == cls and w["normal"] for w in ctl.kwin_eval(ctl._WINDOWS_JS)):
        print(f"{cls} not running")
        return 1
    ctl.kwin_raise(cls)
    time.sleep(1.0)
    recorder = Recorder()
    kwin = KWinSource(recorder)
    AtspiSource(recorder)
    BackendSource(recorder)
    ctx = GLib.MainContext.default()
    summaries = []
    try:
        for step in steps:
            result = run_step(ctx, recorder, cls, step)
            s = summarize(result)
            summaries.append(s)
            print(json.dumps(s, ensure_ascii=False), flush=True)
    finally:
        kwin.close()
    if json_out:
        with open(json_out, "w") as f:
            json.dump({"app": app, "steps": summaries}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
