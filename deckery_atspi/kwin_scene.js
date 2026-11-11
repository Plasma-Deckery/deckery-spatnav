// deckery-atspi scene feed (BACKEND_V2_DESIGN.md §3).
//
// Loaded at runtime by deckery_atspi/kwin_source.py through KWin's
// /Scripting D-Bus interface (not installed as a KWin script package), and
// unloaded again when the backend exits. Pushes the full scene — outputs,
// windows in stacking order bottom→top, active window — to the backend on
// every change that affects what is on screen.

var BUS_NAME = "__BUS_NAME__";

function rect(r) {
    try { return r ? {x: r.x, y: r.y, w: r.width, h: r.height} : null; } catch (e) { return null; }
}

var FLAG_NAMES = ["normalWindow", "dialog", "dock", "desktopWindow", "toolbar", "menu",
                  "utility", "splash", "dropdownMenu", "popupMenu", "tooltip", "notification",
                  "criticalNotification", "appletPopup", "onScreenDisplay", "comboBox",
                  "popupWindow", "specialWindow", "fullScreen", "keepAbove", "keepBelow"];

function flags(w) {
    var out = [];
    for (var i = 0; i < FLAG_NAMES.length; i++) {
        try { if (w[FLAG_NAMES[i]]) out.push(FLAG_NAMES[i]); } catch (e) {}
    }
    return out;
}

function windowJson(w) {
    var desktops = [];
    try { for (var j = 0; j < w.desktops.length; j++) desktops.push(w.desktops[j].id); } catch (e) {}
    var output = null;
    try { output = w.output ? w.output.name : null; } catch (e) {}
    return {
        id: String(w.internalId),
        pid: w.pid,
        caption: w.caption,
        resourceClass: String(w.resourceClass),
        layer: (function () { try { return w.layer; } catch (e) { return -1; } })(),
        output: output,
        frame: rect(w.frameGeometry),
        client: rect(w.clientGeometry),
        buffer: rect(w.bufferGeometry),
        desktops: desktops,
        onAllDesktops: !!w.onAllDesktops,
        minimized: !!w.minimized,
        hidden: !!w.hidden,
        flags: flags(w)
    };
}

function push() {
    var outputs = [];
    var screens = workspace.screens;
    for (var i = 0; i < screens.length; i++) {
        outputs.push({name: screens[i].name, geometry: rect(screens[i].geometry),
                      scale: screens[i].devicePixelRatio});
    }
    var windows = [];
    var stack = workspace.stackingOrder;
    for (var i = 0; i < stack.length; i++) {
        if (stack[i].deleted) continue;
        windows.push(windowJson(stack[i]));
    }
    callDBus(BUS_NAME, "/Scene", "org.deckery.Atspi.Scene", "Update", JSON.stringify({
        version: 2,
        currentDesktop: workspace.currentDesktop ? workspace.currentDesktop.id : null,
        activeWindow: workspace.activeWindow ? String(workspace.activeWindow.internalId) : null,
        outputs: outputs,
        windows: windows
    }));
}

function hook(w) {
    var signals = ["frameGeometryChanged", "minimizedChanged", "desktopsChanged",
                   "hiddenChanged", "captionChanged", "outputChanged"];
    for (var i = 0; i < signals.length; i++) {
        if (w[signals[i]]) w[signals[i]].connect(push);
    }
}

workspace.windowList().forEach(hook);
workspace.windowAdded.connect(hook);
workspace.windowAdded.connect(push);
workspace.windowRemoved.connect(push);
workspace.windowActivated.connect(push);
workspace.currentDesktopChanged.connect(push);
if (workspace.stackingOrderChanged) workspace.stackingOrderChanged.connect(push);
if (workspace.screensChanged) workspace.screensChanged.connect(push);
push();
