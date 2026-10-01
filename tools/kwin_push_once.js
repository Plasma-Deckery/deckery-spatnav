// One-off snippet (deckery#28): pushes the current window list exactly
// once, for a Python client to request via KWin's generic /Scripting
// D-Bus interface right after it starts — so it doesn't have to wait for
// the persistent deckery-windowtracker script's next natural event.
function snapshotWindows() {
    var wins = workspace.windowList();
    var out = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var desktopIds = [];
        try {
            if (w.desktops) {
                for (var j = 0; j < w.desktops.length; j++) {
                    desktopIds.push(w.desktops[j].id);
                }
            }
        } catch (e) {}
        var clientGeo = null, bufferGeo = null, stackingOrder = -1;
        try {
            if (w.clientGeometry) {
                clientGeo = {x: w.clientGeometry.x, y: w.clientGeometry.y,
                             width: w.clientGeometry.width, height: w.clientGeometry.height};
            }
        } catch (e) {}
        try {
            if (w.bufferGeometry) {
                bufferGeo = {x: w.bufferGeometry.x, y: w.bufferGeometry.y,
                             width: w.bufferGeometry.width, height: w.bufferGeometry.height};
            }
        } catch (e) {}
        try { stackingOrder = w.stackingOrder; } catch (e) {}
        out.push({
            pid: w.pid, caption: w.caption, desktops: desktopIds,
            onAllDesktops: !!w.onAllDesktops, minimized: !!w.minimized,
            hidden: !!w.hidden, normalWindow: !!w.normalWindow,
            clientGeometry: clientGeo, bufferGeometry: bufferGeo,
            stackingOrder: stackingOrder
        });
    }
    return out;
}
var payload = {
    currentDesktop: workspace.currentDesktop ? workspace.currentDesktop.id : null,
    windows: snapshotWindows()
};
callDBus("org.deckery.WindowTracker", "/WindowTracker",
         "org.deckery.WindowTracker", "Update", JSON.stringify(payload));
