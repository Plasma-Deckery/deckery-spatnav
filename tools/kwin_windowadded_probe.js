// Persistent diagnostic (MENU_HIJACK_DESIGN.md §12): logs every
// workspace.windowAdded event via callDBus to our debug listener, to check
// whether the Window Menu (titlebar icon / top-left window decoration
// popup) appears as a scriptable "Window" object at all — if it never
// fires windowAdded, there is nothing to set keepBelow/layer on, settling
// the Z-order question definitively instead of guessing.
function report(w) {
    var geo = w.frameGeometry || w.clientGeometry || {};
    var payload = {
        pid: w.pid,
        caption: w.caption,
        resourceClass: w.resourceClass,
        normalWindow: !!w.normalWindow,
        hasKeepBelow: (typeof w.keepBelow !== "undefined"),
        keepBelow: w.keepBelow,
        hasLayer: (typeof w.layer !== "undefined"),
        layer: w.layer,
        x: geo.x, y: geo.y, width: geo.width, height: geo.height
    };
    callDBus("org.deckery.Debug", "/Debug", "org.deckery.Debug", "Dump", JSON.stringify([payload]));
}

workspace.windowAdded.connect(report);
