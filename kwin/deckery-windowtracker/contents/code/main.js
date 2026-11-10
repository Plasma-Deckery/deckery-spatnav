// Deckery Window Tracker
//
// Wayland gives no public D-Bus call for "list all windows + which virtual
// desktop they're on" — that information only exists inside KWin's internal
// scripting API (workspace.windowList()). This script is the bridge: it
// pushes the current window list to an external D-Bus service whenever
// anything relevant changes, so processes outside KWin (like the deckery
// spatial-nav overlay prototype, deckery#28) can know what's actually
// visible on the active desktop right now.
//
// Push, not poll: reacts to windowAdded/windowRemoved/currentDesktopChanged/
// minimizedChanged/hiddenChanged instead of being queried on a timer.

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
        } catch (e) {
            // ignore, leave desktopIds empty
        }
        // clientGeometry is the window's on-screen client-area rectangle in
        // the compositor's real (rotated + HiDPI-scaled) output coordinate
        // space — this display reports Scale: 1.1 and Rotation: 8 (portrait
        // panel software-rotated to landscape), and Qt's AT-SPI bridge does
        // NOT apply that transform: Atspi CoordType.SCREEN on this system
        // actually returns coordinates relative to the window's own client
        // area (same numbers as CoordType.WINDOW), confirmed by comparing
        // Dolphin's AT-SPI top-level frame extents (0,0,572,657) against
        // this clientGeometry (x=585,y=64,w=572,h=657) — sizes match, so the
        // overlay adds this x/y back in as the missing offset (deckery#28).
        //
        // BUT for GTK apps using client-side decorations (Firefox, GTK4
        // apps), AT-SPI's element-relative coordinate origin is anchored to
        // the surface BUFFER's top-left, not clientGeometry's — the buffer
        // includes an invisible shadow/resize-handle margin around the
        // visible content that clientGeometry excludes. Using clientGeometry
        // as the offset therefore overshoots by exactly that margin (~26px
        // left, ~23px top measured for Firefox on this system), pushing
        // every drawn rectangle down-and-right by a constant amount that
        // matched neither a titlebar (frameGeometry == clientGeometry here,
        // so no titlebar) nor a scale bug (same magnitude everywhere, not
        // proportional to distance from origin). bufferGeometry is the
        // correct offset source; clientGeometry is kept alongside for
        // width/height and for apps where the two coincide (e.g. Dolphin,
        // which has no CSD margin) (deckery#28).
        var clientGeo = null;
        var bufferGeo = null;
        try {
            if (w.clientGeometry) {
                clientGeo = {
                    x: w.clientGeometry.x,
                    y: w.clientGeometry.y,
                    width: w.clientGeometry.width,
                    height: w.clientGeometry.height
                };
            }
        } catch (e) {
            // ignore, leave clientGeo null
        }
        try {
            if (w.bufferGeometry) {
                bufferGeo = {
                    x: w.bufferGeometry.x,
                    y: w.bufferGeometry.y,
                    width: w.bufferGeometry.width,
                    height: w.bufferGeometry.height
                };
            }
        } catch (e) {
            // ignore, leave bufferGeo null
        }
        // hidden is true for auto-hide panels that are currently tucked away
        // (slid off-screen) — distinct from minimized (which means "in the
        // taskbar") and from being on another desktop. KWin sets hidden=true
        // on the window when an auto-hide panel is not currently showing,
        // even though the window itself still exists. !!w.hidden is safe to
        // evaluate even if the property doesn't exist on some KWin builds —
        // !!undefined === false, so consumers get false (= not hidden) rather
        // than an error.
        // stackingOrder is a per-window numeric z-position (higher = on top).
        // Used by the overlay to skip drawing elements of windows that are
        // fully occluded by a higher-stacked window (B3 occlusion fix).
        var stackingOrder = -1;
        try { stackingOrder = w.stackingOrder; } catch (e) {}

        out.push({
            pid: w.pid,
            caption: w.caption,
            desktops: desktopIds,
            onAllDesktops: !!w.onAllDesktops,
            minimized: !!w.minimized,
            hidden: !!w.hidden,
            normalWindow: !!w.normalWindow,
            clientGeometry: clientGeo,
            bufferGeometry: bufferGeo,
            stackingOrder: stackingOrder
        });
    }
    return out;
}

function pushUpdate() {
    var payload = {
        currentDesktop: workspace.currentDesktop ? workspace.currentDesktop.id : null,
        // Which real application is currently focused, by pid. Used by
        // menu_badge_prototype.py to correct the Global Menu dropdown
        // (app='kded6', always reports (0,0) via AT-SPI — a confirmed bug in
        // that component): kded6 itself can hold several same-sized ~1x1px
        // anchor placeholder windows at once (one per app that has ever used
        // the global menu this session, some possibly stale/left over), so
        // there is no reliable way to tell which one belongs to the menu
        // that's actually open right now just from kded6's own window list
        // (deckery#28). The Global Menu always shows the ACTIVE window's
        // menu, so its real on-screen position is better approximated by
        // the active window's own (already correctly, live-tracked) offset
        // than by guessing among kded6's own anchor windows.
        activePid: workspace.activeWindow ? workspace.activeWindow.pid : null,
        windows: snapshotWindows()
    };
    callDBus(
        "org.deckery.WindowTracker",
        "/WindowTracker",
        "org.deckery.WindowTracker",
        "Update",
        JSON.stringify(payload)
    );
}

workspace.windowAdded.connect(pushUpdate);
workspace.windowRemoved.connect(pushUpdate);
workspace.currentDesktopChanged.connect(pushUpdate);
workspace.windowActivated.connect(pushUpdate);

// minimizedChanged/frameGeometryChanged are per-window signals; hook them on
// every window we see. frameGeometryChanged covers move/resize, so the
// overlay's screen-position offset (see clientGeometry above) stays correct
// while a window is dragged around instead of only updating on the next
// REFRESH_SECONDS AT-SPI walk.
function hookWindow(w) {
    if (w.minimizedChanged) {
        w.minimizedChanged.connect(pushUpdate);
    }
    if (w.desktopsChanged) {
        w.desktopsChanged.connect(pushUpdate);
    }
    if (w.frameGeometryChanged) {
        w.frameGeometryChanged.connect(pushUpdate);
    }
    if (w.hiddenChanged) {
        w.hiddenChanged.connect(pushUpdate);
    }
}
workspace.windowList().forEach(hookWindow);
workspace.windowAdded.connect(hookWindow);

// Initial push so a consumer that starts after this script don't have to
// wait for the next window event.
pushUpdate();

// --- "Next window to last desktop" one-shot placement ---
// Activated via kglobalaccel shortcut "deckery-next-on-last-desktop",
// which is invoked by tools/next-on-last-desktop.sh.
// Sets a flag that fires exactly once on the next normalWindow that opens,
// moves it to the last virtual desktop, then resets.
var sendNextToLast = false;

registerShortcut(
    "deckery-next-on-last-desktop",
    "Deckery: Send Next Window to Last Desktop",
    "",
    function () {
        sendNextToLast = true;
    }
);

workspace.windowAdded.connect(function (w) {
    if (!sendNextToLast || !w.normalWindow) return;
    sendNextToLast = false;
    var desktops = workspace.desktops;
    if (desktops.length === 0) return;
    var last = desktops[desktops.length - 1];
    w.desktops = [last];
    workspace.currentDesktop = last;
});
