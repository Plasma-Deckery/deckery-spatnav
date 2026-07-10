# AT-SPI2 Coverage Limitations — deckery #28

## Summary

The spatial-navigation overlay can only display focusable UI elements that are:
1. **Registered as top-level windows with AT-SPI2** (app must have AT-SPI support)
2. **Export their internal UI element tree to AT-SPI2** (app must expose DOM-like structure)
3. **Actually be visible on the current desktop** (covered by KWin tracker)

This document covers the gaps where even "accessible" apps fail to expose elements.

---

## Known Gaps

### Electron Apps (e.g., Claude Desktop)

**Status**: Partially AT-SPI-enabled, but **no UI elements exposed**

**Behavior**:
- The Electron app DOES register with AT-SPI2
- AT-SPI reports the window frames (`claude.get_child_count()` returns 2 frames)
- Frame "Claude" appears with valid extents (1164×671)
- **BUT**: Frames have zero children in the AT-SPI tree
- **Result**: Overlay shows 0 focusable elements inside Electron windows

**Technical Root Cause**:
Electron uses the Qt AT-SPI bridge (via `QtCore`/`QtWebEngine`) to expose only the window itself, not its internal DOM.
Electron's web-based UI (rendered as a single WebEngine surface) is not decomposed into individual focusable elements by the AT-SPI bridge.

**What This Means**:
- Electron-based apps (Claude, Discord, VSCode-based apps, etc.) won't have any elements highlighted by the overlay
- The window frame itself is focusable but doesn't help navigate inside it

**Workaround for Users**:
None at the Deckery level. Electron apps would need to:
1. Export keyboard-navigable elements via custom ARIA attributes, then
2. Have Electron/Chromium's AT-SPI implementation recognize and bridge them

This is out of scope for deckery.

---

### KDE Context Menus & Window Menus

**Status**: Exist in AT-SPI but **not tracked by KWin** as top-level windows

**Behavior**:
- Right-click context menus are NOT windows in KWin's `workspace.windowList()`
- Window menu (Ctrl+F2, Alt+F3) is also NOT in `workspace.windowList()`
- KDE panel buttons/menu items ARE in AT-SPI (586 menu-related elements found in plasmashell alone)
- **But actual popup menus** triggered at runtime are transient windows, not tracked by KWin

**Technical Root Cause**:
Context menus and window menus are created as temporary popup surfaces by the window manager or application itself,
not as persistent "normal windows". KWin's public/JavaScript API tracks only "normal windows" (persistent application windows).

**What This Means**:
- Context menu items won't be highlighted in the overlay
- Window menu items (like "Maximize", "Minimize") won't be highlighted
- This is most noticeable when navigating with a gamepad and trying to access right-click context menus

**Workaround for Users**:
- Use keyboard shortcuts instead of context menus when possible
- Some apps provide keyboard access to menu functions directly (e.g., Edit menu via Alt+E)
- deckery could add dedicated gamepad bindings for common context-menu actions (out of scope for MVP)

---

### Other Known Limitations

#### Qt Applications with Complex Dialogs
Some Qt applications may have internal dialogs or dockable panels that AT-SPI doesn't expose if:
- The application creates them as child widgets of the main window rather than top-level windows
- The widget subtree isn't properly marked with AT-SPI states

**Mitigation**: This is application-dependent and can't be fixed in deckery without patching the Qt AT-SPI bridge itself.

#### Wayland-Native Apps Without AT-SPI Support
Some newer Wayland-native applications may not have AT-SPI2 support at all.

**Check**: If an app doesn't appear in `Atspi.get_desktop(0)` or has zero child windows, it's not AT-SPI-enabled.

---

## Testing & Verification

### Check if an app is AT-SPI-enabled:
```bash
python3 << 'EOF'
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if app is None:
        continue
    try:
        name = app.get_name() or '(unnamed)'
        n_windows = app.get_child_count()
        print(f'{name}: {n_windows} window(s)')
    except:
        pass
EOF
```

### Check for focusable elements inside a specific app:
```bash
# Set APP_NAME to the app you want to inspect
APP_NAME="firefox"

python3 << 'EOF'
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

desktop = Atspi.get_desktop(0)
target_app = None

for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if app is None:
        continue
    try:
        if APP_NAME in (app.get_name() or '').lower():
            target_app = app
            break
    except:
        pass

if target_app:
    def count_focusable(node, depth=0):
        count = 0
        try:
            state_set = node.get_state_set()
            if state_set.contains(Atspi.StateType.FOCUSABLE):
                count += 1
            
            n_children = node.get_child_count()
            for i in range(n_children):
                try:
                    child = node.get_child_at_index(i)
                    if child:
                        count += count_focusable(child, depth + 1)
                except:
                    pass
        except:
            pass
        return count
    
    total = count_focusable(target_app)
    print(f'{APP_NAME}: {total} focusable element(s)')
else:
    print(f'{APP_NAME} not found in AT-SPI')
EOF
```

---

## Future Improvements (Out of MVP Scope)

1. **Electron Bridge Enhancement**:
   - Contribute to Chromium/Electron's AT-SPI implementation to expose web-page focus targets
   - Track upstream: https://bugs.chromium.org/p/chromium/issues/detail?id=...

2. **Context Menu Support**:
   - Add KWin script logic to track transient popup windows
   - Requires extending KWin's JS API or hooking window decoration signals

3. **Fallback Keyboard Navigation**:
   - For apps with no AT-SPI elements, use raw keyboard focus chain
   - Track which app is focused, then inject keyboard events to navigate (complex, security-sensitive)

---

## References

- AT-SPI2 Specification: https://wiki.gnome.org/Accessibility/AT-SPI/Interfaces
- Qt AT-SPI Bridge: https://github.com/qt/qtbase/tree/dev/src/plugins/platforms/linux/a11y
- Electron AT-SPI Issues: https://github.com/electron/electron/labels/a11y
