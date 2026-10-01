// One-off diagnostic (MENU_HIJACK_DESIGN.md): dumps every window KWin's
// workspace.windowList() currently knows about, to check whether the Global
// Menu's hosting window (likely owned by kded6 or plasmashell) appears at
// all, and if so, at what reported position — tests the hypothesis that
// its AT-SPI (0,0) position comes from a real KWin-tracked anchor window
// that was simply never positioned, vs. not being tracked by KWin at all
// (REQUIREMENTS.md §18.2's documented "transient popups aren't in
// workspace.windowList()" gap).
var wins = workspace.windowList();
var out = [];
for (var i = 0; i < wins.length; i++) {
    var w = wins[i];
    var geo = w.frameGeometry || w.clientGeometry || {};
    out.push({
        pid: w.pid,
        caption: w.caption,
        resourceClass: w.resourceClass,
        normalWindow: !!w.normalWindow,
        x: geo.x, y: geo.y, width: geo.width, height: geo.height
    });
}
callDBus("org.deckery.Debug", "/Debug", "org.deckery.Debug", "Dump", JSON.stringify(out));
