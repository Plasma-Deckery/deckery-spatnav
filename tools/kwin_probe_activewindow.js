// One-off diagnostic: safely probe whether workspace.activeWindow exists
// and is accessible in this KWin version, without risking breaking the
// persistent deckery-windowtracker script if the property name is wrong
// (deckery#28 — suspected cause of pushUpdate() silently throwing and
// killing ALL Update() pushes after adding an unguarded
// workspace.activeWindow reference to it).
var result = {};
try {
    result.hasActiveWindow = (typeof workspace.activeWindow !== "undefined");
    result.activeWindowValue = workspace.activeWindow ? {
        pid: workspace.activeWindow.pid,
        caption: workspace.activeWindow.caption
    } : workspace.activeWindow;
} catch (e) {
    result.error = String(e);
}
try {
    result.hasActiveClient = (typeof workspace.activeClient !== "undefined");
} catch (e) {
    result.activeClientError = String(e);
}
callDBus("org.deckery.Debug", "/Debug", "org.deckery.Debug", "Dump", JSON.stringify([result]));
