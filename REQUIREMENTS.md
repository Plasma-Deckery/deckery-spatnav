# deckery spatial-nav overlay — Requirements

Präzise Dokumentation aller Designentscheidungen, implementierten Eigenheiten und
Verhaltenserwartungen des Overlay-Systems. Gilt als verbindliche Referenz für
Refactoring, neue Implementierungen und Tests.

---

## 1. Systemüberblick

Das Overlay ist eine vollflächige, transparente GTK4-Fensterschicht (Wayland
layer-shell, OVERLAY-Layer) die über dem gesamten Desktop schwebt und um jedes
aktuell sichtbare, ansteuerbare AT-SPI2-Element eines Fensters ein farbiges
Rechteck zeichnet. Es ist kein Screen Reader — es spricht nichts, steuert
nichts, injiziert keine Eingaben. Es ist eine passive visuelle Hilfe: der
Nutzer sieht, welche Elemente vorhanden sind und wo sie liegen.

Laufumgebung: deckery distrobox-Container (Arch-basiert), der GTK4 + GI-Bindings
für Gtk4LayerShell, AT-SPI2 und Cairo enthält. Der Container teilt Session-DBus
und Wayland-Socket mit dem Host (KDE Plasma).

---

## 2. Layer-Shell-Bootstrap

### 2.1 LD\_PRELOAD Re-exec

`libgtk4-layer-shell.so` muss per `LD_PRELOAD` geladen sein, **bevor** GTK
initialisiert wird, weil gtk4-layer-shell GDK-Surface-Erzeugung intercept. Wird
das so nicht bereitgestellt, initialisiert `Gtk4LayerShell.init_for_window()`
stillschweigend nichts (nur eine Warnung auf stderr), und das Fenster wird ein
normales xdg-toplevel, das der WM schließen/verstecken kann und an einen
einzigen virtuellen Desktop gebunden ist.

**Mechanismus**: Beim Prozessstart prüft das Skript, ob
`libgtk4-layer-shell.so` in `LD_PRELOAD` enthalten ist. Falls nicht, setzt es
die Variable und ruft `os.execvp(sys.executable, [sys.executable] + sys.argv)`
auf — einmalige Neuausführung des eigenen Prozesses mit gesetztem Preload. Dabei
wird zusätzlich `PYTHONUNBUFFERED=1` gesetzt, damit Log-Ausgaben über die
Grenze des Re-exec nicht scheinbar "verschwinden".

### 2.2 Layer-Shell-Konfiguration

| Eigenschaft | Wert | Begründung |
|---|---|---|
| Layer | OVERLAY | Liegt über allen normalen Fenstern |
| Exclusive zone | −1 | Verdrängt keine anderen Fenster aus dem Arbeitsbereich |
| Keyboard mode | NONE | Stehlt keine Tastatureingaben |
| Anchors | TOP + BOTTOM + LEFT + RIGHT | Vollflächig, folgt Bildschirmgröße |

### 2.3 Transparenz

CSS-Klasse `overlay-transparent` mit `background: transparent` via
`Gtk.CssProvider`. Ohne das würde GTK einen Standard-Fensterhintergrund zeichnen.

### 2.4 Click-Through

Das Overlay empfängt keine Pointer-Events. Bei jedem Draw-Aufruf wird die
Wayland-Input-Region der Surface auf eine leere `cairo.Region()` gesetzt. Das
muss bei jedem Draw-Aufruf wiederholt werden, weil die Wayland-Surface bei
Show/Hide-Zyklen neu erstellt werden kann.

---

## 3. AT-SPI-Baum: Aufbau und Pflege

### 3.1 Initialer Vollständigkeits-Walk (einmalig)

`ElementCache.build_initial()` läuft **einmalig** auf einem Hintergrundthread
(mit `ATSPI_LOCK`, einem `threading.RLock`). Er durchläuft den gesamten
AT-SPI-Desktop (`Atspi.get_desktop(0)`) und alle registrierten Applikationen.
Dabei werden libatspi's eigener D-Bus-Protokoll-Cache befüllt, sodass
nachfolgende Render-Aufrufe zu `AXComponent.get_rect()` und
`AXObject.get_state_set()` ohne D-Bus-Roundtrips bedient werden.

Das Ergebnis wird per `GLib.idle_add` an den GTK-Main-Thread übergeben
(`apply_initial()`). Erst danach wird `_tree_ready = True` gesetzt und der
`EventRouter` gestartet.

### 3.2 Lade-Zustand

Während `build_initial()` läuft (5–8 Sekunden), zeichnet das Overlay einen
HUD-Text `"deckery spatial-nav — building AT-SPI tree…"`. Kein schwarzes/leeres
Fenster.

### 3.3 Sicherheits-Rebuild

Alle 60 Sekunden wird `build_initial()` erneut auf einem Hintergrundthread
ausgeführt und das Ergebnis via `apply_initial()` atomar auf den Main-Thread
übertragen. Fängt verlorene Events auf (Verbindungsabbrüche, missed events).

### 3.4 Inkrementelle Event-Pflege

Nach dem Initial-Build wird der Baum **nicht** periodisch neu durchlaufen,
sondern ausschließlich durch AT-SPI-Events gepflegt. Jeder Event-Typ berührt
nur das kleinstmögliche Stück des Caches:

| Event-Typ | Handler | Kosten |
|---|---|---|
| `window:create` | Walk des neuen Fensters, in Cache aufnehmen | O(Fenster) |
| `window:activate` | Walk nur wenn Fenster noch nicht im Cache (fängt fehlende `create`-Events auf). Gibt `False` zurück → kein Render, wenn bereits bekannt | O(Fenster) oder O(1) |
| `window:destroy` | Fenster-Eintrag aus Cache löschen | O(1) |
| `window:deactivate` | **No-op.** Das Fenster existiert noch; nichts zu tun | — |
| `object:children-changed:add` (inkl. `:system`) | Neues Kind und Subtree auf relevante Elemente prüfen; wenn das Kind selbst ein Fenster-Rolle hat → als `window:create` behandeln | O(Subtree) |
| `object:children-changed:remove` (inkl. `:system`) | Spezifisches Objekt aus Cache entfernen; wenn das Kind Fenster-Rolle hat → als `window:destroy` behandeln | O(1) |
| `object:state-changed:showing` | `clear_cache(obj)` → Eligibility neu prüfen; wenn der Objekt selbst ein noch nicht gecachtes Fenster-Frame ist → als `window:create` behandeln | O(1) |
| `object:state-changed:enabled` | wie oben | O(1) |

**Wichtig zu `:system`-Suffix**: KDE plasmashell feuert
`children-changed:add:system` und `children-changed:remove:system` wenn
Popups (z.B. Application Launcher) erscheinen/verschwinden. Der `:system`-Suffix
bedeutet app-intern ausgelöste Baumänderung. Das Event-Routing verwendet
`startswith()` statt `==`, damit diese Varianten korrekt geroutet werden.

### 3.5 Libatspi-Cache-Invalidierung

Bei `children-changed:add`-Events wird `AXObject.clear_cache(event.source)`
aufgerufen, um libatspi's Protokoll-Cache für das Elternobjekt zu invalidieren
— damit `get_child_count()`/`get_child_at_index()` das neue Kind beim nächsten
Aufruf sehen. Bei `state-changed`-Events wird `AXObject.clear_cache(obj)` für
das geänderte Objekt aufgerufen. Beides ist gezielt (ein Objekt), kein globales
Cache-Clear.

---

## 4. Element-Eligibility (`is_relevant_element`)

Ein AT-SPI-Accessible-Objekt wird ins Overlay aufgenommen, wenn **alle**
folgenden Bedingungen erfüllt sind — außer bei den unten genannten Ausnahmen:

1. **Nicht dead** (`AXObject.is_dead()` → False)
2. **SHOWING** (`AXUtilitiesState.is_showing()` → True)
3. **ENABLED** (`AXUtilitiesState.is_enabled()` → True)
4. **FOCUSABLE** (`AXUtilitiesState.is_focusable()` → True)
   **ODER** Rolle in `{"list item", "table cell", "tree item"}` (Fallback, s.u.)

### 4.1 Ausnahme: Benannte Menü-Knoten (Firefox Menüleiste)

Firefox' Menüleisten-Einträge (File, Edit, View) haben Rolle `"menu"` und
setzen `is_showing=False` auch wenn die Leiste sichtbar ist — AT-SPI-Quirk in
Firefox' Gecko-Bridge. Gleichzeitig haben sie einen nicht-leeren Namen.

**Regel**: Knoten mit `role="menu"` UND nicht-leerem Namen → SHOWING-Check wird
übersprungen, nur ENABLED wird geprüft.

Knoten mit `role="menu"` und **leerem** Namen sind versteckte Dropdown-Menüs,
die Firefox bereits im AT-SPI-Baum vorinstanziiert (Inhalt, aber nicht sichtbar).
Diese bekommen den normalen SHOWING+ENABLED-Check und erscheinen erst, wenn der
Nutzer das Menü öffnet (dann wird `state-changed:showing` mit `True` gefeuert).

### 4.2 Fallback: QML/Kirigami List Rows

In QML/Kirigami-Anwendungen (z.B. KDE System Settings Sidebar) sind einzelne
Listenzeilen nie selbst FOCUSABLE — nur der Container-Liste wird FOCUSABLE
zugewiesen. Trotzdem sind die einzelnen Zeilen per Pfeiltasten navigierbar.

**Fallback**: Knoten mit Rolle in `{"list item", "table cell", "tree item"}`, die
SHOWING und ENABLED sind, werden auch ohne FOCUSABLE aufgenommen.

---

## 5. Event-Spam-Filterung

Vor der Verarbeitung jedes Events entscheidet `EventSpamFilter.should_ignore()`
ob das Event verworfen wird:

### 5.1 Dedup-Fenster (100 ms)

Selber Event-Typ + selbe App innerhalb von 100 ms → zweites Event verworfen.
Verhindert Flood-Verarbeitung bei Apps die viele redundante Events feuern.
Key = `event.type` (vollständiger String inkl. Suffix).

### 5.2 mutter-x11-frames

Events von der App `"mutter-x11-frames"` werden immer verworfen. Das ist ein
synthetischer XWayland-Frame-Shim unter KWin, der AT-SPI-Events erzeugt die
keine echten UI-Änderungen repräsentieren.

### 5.3 Übergroße Text-Inserts

`object:text-changed:insert` mit > 1000 Zeichen (> 5000 für `text`-Rolle)
→ verworfen. Zeigt massenhaften Content-Load an, nicht inkrementelle Edits.

### 5.4 Null/Dead Children

`object:children-changed` mit `event.any_data == None` oder
`AXObject.is_dead(any_data)` → verworfen.

### 5.5 Uninteressante state-changed:showing

`object:state-changed:showing` wird nur verarbeitet wenn die Rolle des
Quell-Objekts in der Allowlist steht:

```
ALERT, ANIMATION, DIALOG, FRAME, INFO_BAR, MENU, NOTIFICATION,
POPUP_MENU, STATUS_BAR, TOOL_TIP, WINDOW
```

Alle anderen Rollen feuern `state-changed:showing` ständig als Nebeneffekt
normaler Layout-Operationen. **Wichtig**: `FRAME`, `WINDOW` und `POPUP_MENU`
sind in der Liste — Orca's Original-Allowlist war speech-output-orientiert und
enthielt diese nicht (Orca erfährt von Fenstern über `window:create`/`destroy`,
nicht über `state-changed`). Deckery benötigt sie, weil manche KDE-Popups ohne
`window:create` erscheinen und nur über `state-changed:showing` erkannt werden.

---

## 6. Render-Debounce

AT-SPI-Events triggern **keinen** direkten Render-Aufruf. Stattdessen:

1. `_on_event()` ruft `_schedule_change()` auf
2. `_schedule_change()` prüft `_render_pending`: wenn bereits gesetzt → kein
   weiteres `idle_add`
3. Andernfalls: `_render_pending = True`, `GLib.idle_add(_fire_change)`
4. `_fire_change()` setzt `_render_pending = False` und ruft `on_change()` auf

**Effekt**: Egal wie viele Events in einem GLib-Loop-Durchlauf ankommen
(Firefox feuert beim Aufbau seines AT-SPI-Baums tausende
`children-changed`-Events), findet maximal ein Render-Aufruf statt, und zwar
erst wenn der Loop wieder idle ist.

---

## 7. KWin Window Tracker

### 7.1 Problem

AT-SPI2's `VISIBLE`/`SHOWING`-States sind **nicht** virtuell-Desktop-bewusst:
ein Fenster auf einem anderen KDE-Desktop meldet trotzdem `VISIBLE=True`,
`SHOWING=True`. Wayland hat keine öffentliche D-Bus-API für "welche Fenster sind
auf welchem Desktop". Diese Information existiert nur im KWin-Scripting-API.

### 7.2 Architektur

Ein KWin-JavaScript-Skript
(`~/.local/share/kwin/scripts/deckery-windowtracker`) läuft innerhalb von KWin
und pushed die aktuelle Fensterliste per D-Bus bei jeder relevanten Änderung.

Das Python-Overlay registriert den D-Bus-Dienst `org.deckery.WindowTracker` auf
`/WindowTracker` und empfängt `Update(payload_json)`-Aufrufe.

D-Bus-Session wird zwischen Host und deckery-Container geteilt (kein
Protokoll-Overhead durch Container-Grenze).

### 7.3 KWin-Events die einen Push auslösen

- `workspace.windowAdded`
- `workspace.windowRemoved`
- `workspace.currentDesktopChanged`
- Pro Fenster (für alle Fenster in `windowList()` + neue via `windowAdded`):
  - `minimizedChanged`
  - `desktopsChanged`
  - `frameGeometryChanged` (Move/Resize → Offset wird sofort korrekt)
  - `hiddenChanged` (Auto-hide-Panel rein/raus)

### 7.4 Payload-Format

```json
{
  "currentDesktop": "<uuid>",
  "windows": [
    {
      "pid": 12345,
      "caption": "Dolphin",
      "desktops": ["<uuid1>"],
      "onAllDesktops": false,
      "minimized": false,
      "hidden": false,
      "normalWindow": true,
      "clientGeometry": {"x": 100, "y": 50, "width": 800, "height": 600},
      "bufferGeometry": {"x": 74, "y": 27, "width": 852, "height": 649}
    }
  ]
}
```

**`normalWindow` wird absichtlich nicht ausgewertet**, weil Plasma-Panels,
Systray und Taskbar `normalWindow=false` setzen. Würde man darauf filtern,
würden Panels/Taskbar nie im Overlay erscheinen.

### 7.5 Debounce auf Empfänger-Seite

150 ms Debounce zwischen `Update()`-Eingang und Render-Auslösung. Kumuliert
die mehreren KWin-Signale, die für eine einzelne Nutzeraktion aufeinander folgen
können (z.B. `windowAdded` + `frameGeometryChanged` bei einem neuen Fenster).

### 7.6 Fallback ohne Tracker

Wenn `visible_pids is None` (erster `Update()` noch nicht eingetroffen) →
alle Fenster werden als sichtbar betrachtet. Besser zu viel zeigen als gar nichts.

---

## 8. Virtuelle Desktop-Filterung

### 8.1 Haupt-Check

Ein Fenster wird angezeigt wenn:
- `pid` ∈ `visible_pids` (auf aktuellem Desktop, nicht minimiert, nicht auf
  anderem Desktop)

### 8.2 Caption-Tiebreaker

Wenn eine App mehrere Top-Level-Fenster auf **verschiedenen** Desktops hat
(z.B. System Settings mit mehreren offenen Panels), reicht PID-Mitgliedschaft
nicht aus — `pid ∈ visible_pids` wäre True wegen irgendeines der Fenster.

Dann: Wenn der AT-SPI-Fenster-Name in den bekannten Captions für diese PID
vorkommt → nutze den per-Caption-Visibility-Check statt des PID-weiten.

Wenn der AT-SPI-Name nicht in KWin's Captions vorkommt (AT-SPI exponiert manchmal
interne Sub-Panels als eigene Top-Level-Frames, die KWin nicht sieht, z.B.
eine "Sidebar"-Pane) → fallback auf PID-weiten Visibility-Status.

---

## 9. Koordinatensystem und Offsets

### 9.1 AT-SPI liefert fensterrelative Koordinaten

AT-SPI `CoordType.WINDOW` wird explizit verwendet. Auf diesem System liefert
`CoordType.SCREEN` dieselben Zahlen wie `WINDOW` (Qt's AT-SPI-Bridge wendet den
Display-Scale und die Software-Rotation nicht an). Alle Element-Koordinaten
sind relativ zur eigenen Fenster-Client-Area, nicht zum Bildschirm.

Der KWin-Tracker liefert den fehlenden Screen-Offset, der zu jedem Element
addiert werden muss.

### 9.2 bufferGeometry vs. clientGeometry

Für GTK/CSD-Apps (Firefox, GTK4-Anwendungen) liegt der AT-SPI-Koordinatenursprung
an der **Buffer**-Top-Left, nicht an `clientGeometry`-Top-Left. Der Buffer
enthält einen unsichtbaren Shadow/Resize-Handle-Rand (~26 px links, ~23 px oben
bei Firefox auf diesem System), den `clientGeometry` ausschließt.

`clientGeometry` als Offset würde jedes Rechteck um genau diesen Rand nach
unten-rechts verschieben.

**Regel**: `bufferGeometry` wird als primäre Offset-Quelle verwendet.
`clientGeometry` ist Fallback wenn `bufferGeometry` nicht vorhanden. Für
Nicht-CSD-Apps (z.B. Dolphin) sind beide identisch → kein Unterschied.

### 9.3 Offset-Lookup-Priorität (`resolve_offset`)

In dieser Reihenfolge:

1. Exact `(pid, caption)` — aber **nur** wenn `caption` nicht leer ist
2. Size-Key `(pid, round(width), round(height))` — wenn `caption` leer ist
   (z.B. plasmashell, dessen alle Fenster "" als AT-SPI-Name melden)
3. PID-only Fallback (letzter bekannter Offset für diese PID)
4. Caption-only global Fallback (PID-unabhängig, für Flatpak-Apps, s.u.)
5. `(0, 0)` — letzter Ausweg, zeichnet am falschen Ort

**Warum kein `(pid, "")` als Key**: Alle leeren-Caption-Fenster einer App
würden auf denselben Key kollabieren, der letzte gewinnt. Damit hätten alle
plasmashell-Panels denselben (falschen) Offset.

---

## 10. Chromium/Electron-Scale-Korrektur

### 10.1 Problem

Display-Scale auf diesem System: `1.1`. Chromium's WebContents-AT-SPI-Bridge
meldet Element-Positionen in CSS/DIP-Pixeln (unkomprimierter Koordinatenraum),
während alle anderen Toolkits (Qt, Gecko) bereits den
compositorskalierten logischen Raum verwenden.

**Effekt**: Je weiter ein Element von der Fenster-Ecke entfernt ist, desto stärker
driftet die gezeichnete Box nach rechts/unten — klassische Signatur von
~1.1× zu großen lokalen Koordinaten.

### 10.2 Korrektur

Für Chromium-Apps: Element-Koordinaten werden durch `DISPLAY_SCALE` (1.1)
**geteilt** bevor der KWin-Screen-Offset addiert wird.

```python
x = round(extents.x / DISPLAY_SCALE + offset_x)
y = round(extents.y / DISPLAY_SCALE + offset_y)
w = round(extents.width / DISPLAY_SCALE)
h = round(extents.height / DISPLAY_SCALE)
```

### 10.3 Ausnahme: Native Window-Frame-Buttons

`Minimize`, `Maximize`, `Restore`, `Close` — diese Buttons werden von Chromium's
eigenem Titlebar-Code gezeichnet, nicht vom Web-Renderer. Sie melden ihre
Koordinaten bereits im compositor-skalierten Raum. Für sie wird die Division
durch `DISPLAY_SCALE` **nicht** angewendet.

### 10.4 Detektion

`is_chromium_toolkit()`: prüft `AXObject.get_toolkit_name()` auf `"chromium"`
(case-insensitiv; Orca's AXObject lowercased den Namen bereits).

Nur Chromium bekommt die Korrektur. Qt und Gecko laufen ohne sie korrekt.

---

## 11. AT-SPI-Aktivierung für Chromium und Firefox

### 11.1 Chromium/Electron

Chromium hält seinen AT-SPI-Baum per Default deaktiviert (Performance). Das
Fenster ist in AT-SPI registriert und hat korrekte Extents, aber **keine Kinder**
— Chromium baut den Baum schlicht nicht auf.

**Lösung**: Beim Overlay-Start wird per D-Bus
`org.a11y.Bus` → `/org/a11y/bus` → `org.freedesktop.DBus.Properties.Set`
aufgerufen:

```
("org.a11y.Status", "IsEnabled", True)
```

Chromium's Aura Linux Accessibility Backend reagiert auf `IsEnabled=True`
indem es für alle offenen Fenster volle Renderer-Accessibility-Trees aufbaut.
Einmalig, feuert einmal beim Start.

### 11.2 Firefox/Gecko

Firefox reagiert nicht auf `IsEnabled` allein. Es benötigt
`ScreenReaderEnabled=True` um seinen vollen AT-SPI-Baum (Tabs, Toolbar,
Adressleiste, Seiten-Inhalt) zu initialisieren. Ohne es ist nur der
Top-Level-Fenster-Frame registriert, null Kinder exposed.

**Lösung**: Beide Properties werden gesetzt:

```python
("org.a11y.Status", "IsEnabled", True)        # Chromium/Electron
("org.a11y.Status", "ScreenReaderEnabled", True)  # Firefox/Gecko
```

**Sicherheit auf KDE**: Auf GNOME würde `ScreenReaderEnabled=True` durch
`gsd-a11y-settings` in einen automatischen Orca-Start gespiegelt. Auf diesem
KDE-Plasma-System existiert kein `gsd-a11y-settings` → kein Seiteneffekt.

---

## 12. Sandboxed Apps / Flatpak (PID-Mismatch)

Firefox läuft als Flatpak. AT-SPI's `get_process_id()` gibt die PID des
`xdg-dbus-proxy` zurück (die Sandbox-Brücke), **nicht** die PID des echten
Firefox-Prozesses die KWin sieht.

**Erkennung**: Wenn `AT-SPI-PID ∉ all_known_pids` (KWin hat diese PID nie
gemeldet), gilt die PID-Korrelation als gebrochen für dieses Fenster. Nicht zu
verwechseln mit "bekannte PID, aber gerade auf anderem Desktop".

**Fallback**: Caption-only-Sichtbarkeits- und Offset-Lookup (PID-unabhängig).
Der Window-Caption ist die einzige verlässliche Verbindung.

---

## 13. plasmashell-Sonderbehandlung

### 13.1 Unzuverlässige AT-SPI-States

KDE plasmashell's Panels und Popups (App-Launcher, Systray-Flyouts,
Clipboard-Popup, Notification-Popups) sind QML-Surfaces deren
`VISIBLE`/`SHOWING`-States "stecken bleiben": manche melden permanent
`VISIBLE=False/SHOWING=False` obwohl sie sichtbar sind, andere melden permanent
`VISIBLE=True/SHOWING=True` obwohl sie geschlossen sind.

**Regel**: Für Apps in `UNRELIABLE_VISIBILITY_APPS` (`"plasmashell"`) wird der
AT-SPI-State-Check ersetzt durch:
`frame_has_valid_extents()` — ein Fenster wird gezeichnet, wenn es jemals mit
nicht-null-Größe ausgelegt wurde.

Nebeneffekt: ein geschlossenes Popup kann an seiner letzten bekannten Position
eine Box zeigen, bis es wieder geöffnet wird. Das wurde als akzeptabel bewertet.

### 13.2 Leere Captions

Alle plasmashell-Fenster melden `""` als AT-SPI-Name. Deshalb:
- Kein `(pid, caption)`-Lookup für plasmashell (würde kollabieren)
- Stattdessen Size-Key-Lookup `(pid, round(w), round(h))`

### 13.3 KWin trackt plasmashell-Popups nicht

`workspace.windowList()` im KWin-Script enthält plasmashell-Popups (App-Launcher,
Systray-Flyouts) nicht — sie sind transiente Popup-Surfaces, keine normalen
Fenster in KWin's Tracking. Es gibt daher keinen KWin-Visibility-Check für
diese Popups; sie werden ausschließlich über AT-SPI-Events (children-changed,
state-changed) verwaltet.

---

## 14. Auto-hide-Panel-Erkennung

KWin setzt `hidden=true` für Auto-hide-Panels die gerade off-screen sind (nicht
sichtbar, aber noch existent — anders als `minimized`, das "in der Taskbar"
bedeutet).

Das Overlay **schließt diese Panels aus**, wenn `hidden=true` im KWin-Payload.

Key: `(pid, round(width), round(height))` — gleicher Mechanismus wie
plasmashell-Offset-Lookup, weil alle plasmashell-Panels leere Captions haben.

Der `hidden`-Wert wird im KWin-Script via `!!w.hidden` ausgewertet.
`!!undefined === false` → bei fehlender Property sehen Konsumenten `false`
(sicherer No-op für ältere KWin-Versionen).

---

## 15. Fenster-Sichtbarkeit: Lazy frame\_extents

`_extract_window_meta()` läuft auf dem Hintergrundthread während `build_initial()`
— bevor AT-SPI für alle Apps vollständig initialisiert ist.
`AXComponent.get_rect(window)` kann zu diesem Zeitpunkt `(0,0,0,0)` zurückgeben.

`frame_extents` wird deshalb **lazy** in `_WindowMeta` gecacht:
- Beim ersten Render: wenn `wm.frame_extents is None`, wird
  `AXComponent.get_rect(wm.window)` erneut aufgerufen
- Bei Erfolg (nicht-null-Größe): Ergebnis in `wm.frame_extents` gespeichert
- Alle nachfolgenden Renders nutzen den Cache ohne D-Bus-Call

---

## 16. Render-Logik

### 16.1 Render-Auslösung

Render wird ausgelöst durch:
- KWin Tracker `Update()` (nach 150 ms Debounce)
- AT-SPI-Event (nach GLib.idle_add-Debounce, max. ein Render pro Loop)
- Nach `apply_initial()` (einmalig beim Startup)

### 16.2 `render_tree()` ist ein reiner Transform

Zero AT-SPI-Calls (alle Rect-Calls sind libatspi-Cache-Hits aus dem
Initial-Walk). Liest `ElementCache`, wendet `deckery_filter.py`-Logik an,
gibt eine Liste `(depth, role, name, extents)` zurück.

Iteration über `list(cache.windows)` und `list(cache.elements_for(...))` —
**Snapshots** verhindern `RuntimeError: dictionary changed size during iteration`:
D-Bus-Calls im Render-Loop spinnen den GLib-Event-Loop, wodurch AT-SPI-Events
feuern können, die den Cache währenddessen modifizieren.

### 16.3 Farbkodierung nach Rolle

| Rolle | Farbe |
|---|---|
| push button / button / push button menu | Grün |
| radio button / check box / slider | Orange |
| menu item | Blau |
| list item | Lila |
| entry / text | Rot |
| Alles andere | Grau |

### 16.4 Labels

Pro Element: Rolle + Name (maximal 30 Zeichen, sonst gekürzt mit `…`) als
kleines Label knapp über dem Rechteck.

### 16.5 HUD (immer sichtbar)

Weiße Statuszeile oben links mit:
- Anzahl der gezeichneten Elemente
- Letzte Render-Dauer in ms
- Anzahl übersprungener (unsichtbarer) Fenster
- Tracker-Status (`"tracker: live"` oder `"tracker: NO DATA (unfiltered)"`)

---

## 17. Threading-Modell

| Phase | Thread | Lock |
|---|---|---|
| `build_initial()` | Hintergrundthread | `ATSPI_LOCK` (RLock) |
| `apply_initial()` | GTK-Main-Thread (via `GLib.idle_add`) | — |
| AT-SPI-Event-Handler | GTK-Main-Thread | — |
| `render_tree()` | GTK-Main-Thread | — |
| Periodischer Rebuild | Hintergrundthread | `ATSPI_LOCK` |
| KWin Tracker `Update()` | GTK-Main-Thread (D-Bus via GLib main loop) | — |

Nach dem Startup läuft alles auf dem Main-Thread — kein Locking nötig außer
während des Hintergrund-Rebuilds.

`_tree_ready`-Flag in `OverlayWindow` verhindert, dass `_render()` und `_draw()`
auf den Cache zugreifen bevor `apply_initial()` gelaufen ist.

---

## 18. Bekannte Grenzen (Out of Scope für dieses Prototype)

### 18.1 Electron/Claude Desktop/Discord/VSCode

`IsEnabled=True` aktiviert zwar Chromium's Renderer-Accessibility, aber
Electron-Apps (mit CEF/Chrome-Embedded-Framework statt Chromium's eigenem
Browser-UI) bauen trotzdem oft keine AT-SPI-Kinder auf. Das Fenster-Frame ist
sichtbar, Elemente innen: 0. Nicht behebbar von Deckery-Seite.

### 18.2 KDE Kontext-Menüs und Fenster-Menüs

Diese sind transiente Popup-Surfaces, die nicht in `workspace.windowList()`
erscheinen. Keine KWin-Korrelation möglich. Kurzlebige Popups die nur per
AT-SPI-Event (`children-changed`) erkennbar wären.

### 18.3 Multi-Monitor

Layer-Shell-Surfaces sind an einen Wayland-Output gebunden. Für mehrere
Monitore wäre ein Overlay-Fenster pro Output nötig. Nicht implementiert.
`DISPLAY_SCALE = 1.1` ist hardcoded für diesen einen Monitor.

### 18.4 DISPLAY\_SCALE nicht dynamisch

Der Scale-Wert wird nicht zur Laufzeit vom System abgefragt (kein
`kscreen-doctor` im Container verfügbar). Hardcoded `1.1`. Der KWin-Tracker
könnte den Output-Scale im Payload mitliefern (er hat Host-seitigen KWin-JS-Zugriff),
das ist aber nicht implementiert.
