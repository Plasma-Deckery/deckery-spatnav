# Backend v2 — Neubau des Element-Backends

**Status:** Entschieden 2026-11-10, Umsetzung läuft. Ersetzt schrittweise
`deckery_backend.py` / `deckery_cache.py` / `deckery_events.py` /
`deckery_walk.py` / `deckery_filter.py`. Die alten Prototypen bleiben lauffähig,
bis v2 sie trägt.

---

## 1. Warum neu

Die Bugs des bisherigen Backends gehen fast alle auf fünf Grundsatzentscheidungen
zurück (Analyse vom 2026-11-10):

1. **Falscher Scope:** alle AT-SPI-Fenster aller Desktops werden gelesen und erst
   danach weggefiltert. Gemessen: 44 AT-SPI-Frames, 9,6 s, 114 Elemente — KWin
   kennt zum selben Zeitpunkt nur 14 echte Fenster. Daraus folgen Desktop-Filter,
   Offset-Raten pro Fenster und Occlusion-Probleme.
2. **Keine stabile Element-Identität:** Kandidaten sind `(role, name, rect)`-Tupel,
   Fokus ist ein Listenindex → Fokus springt nach Umsortierung.
3. **Threads statt Prozessgrenze:** libatspi wird von ≥ 3 Threads benutzt →
   Races, `dbus_watch_handle()`-Crash, Lock-Flickwerk.
4. **Zwei Koordinatenmodelle** (WINDOW + KWin-Offset vs. SCREEN + (0,0)-Gate)
   mit Heuristik-Schichten.
5. **Nicht testbar:** alles wird live per Screenshot geprüft.

## 2. Scope (entschieden)

**Alles, was der Nutzer sehen kann — auf allen angeschlossenen Monitoren.**
Verdeckte Fenster und verdeckte Teile von Fenstern sind keine Ziele.

- Fenster: alle auf dem aktuellen Desktop (bzw. `onAllDesktops`), nicht
  minimiert, nicht `hidden`, mit nicht-leerem sichtbarem Bereich.
- Dazu Panels/Docks, offene Menüs/Popups/Flyouts.
- Ein Element ist Ziel, wenn sein Rechteck — beschnitten auf seine
  Scroll-/Viewport-Vorfahren — sich mit dem sichtbaren Bereich seines Fensters
  überschneidet. Teilverdeckte Elemente: Ziel ist der sichtbare Teil.

## 3. Architektur

```
KWin-Script ──Update(json)──▶ ┌──────────────────────────────┐ ──State──▶ Cursor-Nav-Frontend
(Fenster, Stacking,           │ deckery-atspi (1 Prozess,    │ ──State──▶ Menü-Badges
 Monitore, aktives Fenster)   │ 1 Thread, 1 GLib-MainLoop)   │ ──Event──▶ Makima (später)
                              │                              │
AT-SPI-Events ──────────────▶ │ Szene → Sichtbarkeit →       │
                              │ Elemente → Veröffentlichen   │
                              └──────────────────────────────┘
```

- **Ein Prozess, ein Thread.** Alles AT-SPI-bezogene läuft im Default-MainContext
  dieses Prozesses. Kein `push_thread_default`, keine Locks.
- **Einziger Besitzer** von `org.deckery.WindowTracker`. Frontends sprechen nur
  noch mit dem Backend (`org.deckery.Atspi`), nie selbst mit KWin oder AT-SPI.
- **Ausgabe:** vollständiger State-Snapshot (Liste von Elementen) per D-Bus-Signal
  mit Versionsnummer + Abfragemethode für Neustarts der Konsumenten.

## 4. Pipeline (reine Funktionen, wo möglich)

| Stufe | Eingabe | Ausgabe | Rein? |
|---|---|---|---|
| **Szene** | KWin-Payload | Monitore, Fenster in Stapel-Reihenfolge, Geometrie | ja |
| **Sichtbarkeit** | Szene | pro Fenster: sichtbare Region (Rechteckliste) | ja |
| **Zuordnung** | Szene + AT-SPI-Frames | KWin-Fenster ↔ AT-SPI-Frame | ja |
| **Einlesen** | zugeordnete, sichtbare Frames | Roh-Elemente (fensterrelativ) | nein (AT-SPI) |
| **Platzieren** | Roh-Elemente + Fenster + Toolkit | Bildschirm-Rechtecke | ja |
| **Clippen** | platzierte Elemente + Viewports + Region | Ziele mit sichtbarem Teil | ja |

Alles mit „rein = ja“ wird mit Snapshot-Tests abgedeckt (echte KWin-Payloads und
AT-SPI-Dumps als JSON in `tests/fixtures/`).

## 5. Element-Modell

```
id          "<bus_name>:<object_path>"   stabil, solange das Objekt lebt
window_id   KWin-Fenster-ID
role, name
rect        Bildschirm-Rechteck (logische Pixel), ungeclippt
visible     Liste sichtbarer Teil-Rechtecke (nach Viewport + Occlusion)
target      Zielpunkt (Mitte des größten sichtbaren Teils)
actions     Namen aus dem AT-SPI-Action-Interface (z.B. "click", "press")
```

Die Frontends referenzieren Elemente nur über `id`.

## 6. Koordinaten

Ein einziger Resolver mit benannten Fällen statt Fallback-Kaskade:

- Basis: fensterrelative AT-SPI-Koordinaten + Ursprung des zugeordneten
  KWin-Fensters (`bufferGeometry` bei CSD, sonst `clientGeometry`).
- Monitor-Skalierung kommt aus KWin (`workspace.screens[].devicePixelRatio`),
  nicht mehr hart verdrahtet.
- Toolkit-Sonderfälle als benannte Regeln mit Test (Chromium-WebContents ÷ Scale
  außer Fenster-Buttons, kded6 Global Menu, plasmashell).
- Wenn die Zuordnung Fenster↔Frame nicht eindeutig ist: Fenster **nicht**
  zeichnen und loggen, statt zu raten.

## 7. Aktualisierung

- KWin-Push (Fenster bewegt, gestapelt, Desktop gewechselt) → nur Platzieren +
  Clippen neu, kein AT-SPI-Aufruf.
- Fenster wird sichtbar → Einlesen dieses Fensters.
- AT-SPI-Events (children-changed, state-changed, bounds-changed) → betroffenen
  Teilbaum neu einlesen.
- Gezieltes Polling nur für bekannte Event-lose Fälle (QML-ListViews beim
  Scrollen), begrenzt auf sichtbare Viewports.

## 8. Befunde aus dem KWin-Probe (2026-11-10)

- `workspace.screens` liefert Name, Geometrie, `devicePixelRatio` (eDP-1,
  1164×727, 1.1).
- `workspace.stackingOrder` liefert alle Fenster unten→oben inkl. `layer`
  (0 Desktop, 1 normal, 2 above, 3 Dock, 9 Overlay).
- Geometrien sind fraktional (logische Pixel), z.B. Breite 570.909.
- Popups/Menüs: siehe §9.

## 9. Befunde aus dem Test-Desktop (2026-11-11)

Automatisch gefunden mit `tools/testdesk/` (siehe README dort):

- **Qt-Kontextmenüs** (Dolphin) sind eigene KWin-Popup-Fenster (Layer 6) in der
  Stapel-Reihenfolge → Verdeckung darunter funktioniert ohne Sonderfall. Für
  AT-SPI sind sie **verwaist**: Elternteil = Application, aber
  `index_in_parent = -1`, nicht in deren Kinderliste → nur über ihr
  `object:state-changed:showing`-Event auffindbar. WINDOW-Koordinaten sind
  relativ zum Popup selbst, Größe = KWin-Popup-Größe → normale Zuordnung.
  SCREEN-Koordinaten sind falsch.
- **Qt meldet geteilte Menü-Aktionen falsch** (Cut/Copy/Rename/Properties im
  Kontextmenü): `showing=False` und Geometrie aus dem *anderen* Menü →
  `menu_repair.py` rekonstruiert die Slots aus den korrekt gemeldeten Nachbarn
  (Ziel als `inferred` markiert).
- **Qt-Menüeinträge** sind nie `focusable` → eigene Regel in `eligibility.py`.
- **Größenänderung** ordnet Qt-Inhalt ohne jedes Event neu an → Fenster mit
  geänderter Größe werden neu eingelesen.
- **Kleines Scrollen** in QML-ListViews ohne Delegate-Recycling: kein Event →
  aktives Fenster wird alle 300 ms stichprobenartig live verglichen.
- **Kosten:** ~0,4 ms pro AT-SPI-Aufruf. Qt hat weder ein brauchbares
  `Cache.GetItems` (liefert leer) noch `Collection`. Hebel: Rolle nur einmal,
  kein `is_dead`, Qt-Teilbäume unter `showing=False` nicht betreten → Dolphin
  1,5–2 s → ~250 ms.
- **Dolphin** feuert beim Öffnen eines Menüs ~4000 `state-changed:defunct`.
- **Uhr/CPU-Labels** im Panel feuern Namens-Events im Sekundentakt → werden nur
  noch als Namens-Patch behandelt.
- **GTK4** setzt nie `ENABLED`, nur `SENSITIVE`; Fenster-Koordinaten relativ
  zum Client, nicht zum Buffer (Schattenrand 25 px).
- **Qt-Untermenüs** melden Koordinaten relativ zum Elternmenü → eigene Frames,
  beim Elternteil übersprungen, auf eigene Lage umgerechnet.
- **Plasma 6.6:** Flyouts (Kalender, Tray) **sind** KWin-Fenster
  (`appletPopup`, Layer 3) mit passendem AT-SPI-Frame → keine Sonderbehandlung
  mehr nötig (alte Annahme aus `deckery_filter.py` überholt). Desktop-Icons
  sind benannte `canvas`. Panel-Knöpfe doppelt verschachtelt (gleiches Rechteck).
- **QML-Animationen** ohne Events → ein Nachlese-Durchgang 700 ms nach jedem
  eventgetriebenen Neueinlesen; nachlaufendes Debounce gegen Event-Stürme.
- **Nächster Performance-Hebel:** QML-Flyout (Kalender) 362 Knoten ≈ 1 s →
  gebündelte asynchrone D-Bus-Aufrufe (`GetChildren` etc.) statt libatspi
  Einzelaufrufe.
- **Chromium 156 (Flatpak, 2026-11-12)**, getestet mit Wegwerf-Profil
  (`tools/testdesk/chromium.sh`):
  - Meldet sich ohne Hilfe gar nicht bei AT-SPI an — der Sandbox darf
    `org.a11y.Bus` nicht fragen, ob a11y an ist. `ACCESSIBILITY_ENABLED=1`
    (oder `--talk-name=org.a11y.Bus`) → angemeldet, aber das Fenster bleibt
    leer bis `--force-renderer-accessibility`, auch die Browser-Oberfläche.
    Auf dem echten Desktop ist Chromium deshalb derzeit unsichtbar.
  - Koordinaten: nur der Inhalt von `document web` ist in DIP (÷ Skalierung);
    Tabs, Werkzeugleiste, Adressleiste und Titelleiste nicht. Der Reader
    markiert Web-Elemente und den Web-Teil der Clip-Kette.
  - Echte Tabs (`page tab` in `page tab list`) sind Ziele, obwohl sie einen
    Schließen-Knopf enthalten; die Seite selbst nur, wenn nichts in ihr
    Ziel ist; Optionen eines geschlossenen `<select>` nicht (Chromium meldet
    die gewählte als SHOWING).
  - **Keine Events** außer Fokus/Textcursor/defunct: weder beim Aufbau des
    Baums noch beim Scrollen. Abgefangen durch: leere Frames mit Backoff neu
    lesen, Scroll-Proben (erstes Kind jedes Clip-Containers) im Poll des
    aktiven Fensters, libatspi-Cache vor nicht-eventgetriebenem Lesen
    leeren. Scrollen wird nach ~2 s übernommen.
  - Langsam: ~255 Knoten in 1–1,5 s (durch den Flatpak-a11y-Proxy).
  - Getötete Apps senden kein `window:destroy` → Frame-Liste wird bei
    children-changed der Registry neu geholt (sonst Zuordnung zum toten
    Frame über die gleiche Überschrift).
- Electron: noch keine App installiert, ungetestet.
- Sichtbare Splitter: ein Fenster, das 3 px unter einem anderen hervorschaut,
  liefert 3 px breite Ziele — korrekt nach Scope, für die Navigation
  vermutlich nutzlos (Mindestgröße? offen).
- Nicht getestet: Tastaturfokus.
- KWin-Scripts: `frameGeometry` lässt sich nur aus QML-Scripts setzen
  (`Qt.rect`); einfache Scripts ignorieren die Zuweisung stillschweigend.

## 10. Offene Fragen

1. Plasma-Flyouts (nicht in KWins Fensterliste) — noch nicht im Test-Desktop
   abgedeckt (dort läuft kein plasmashell).
2. Bedienmodell des Cursor-Nav-Frontends: **Springen auf Bewegung**, kein
   fliegender Punkt (Tendenz des Nutzers) — Details offen, eigenes Thema,
   unabhängig vom Backend.
3. Mehrere Monitore: eine Overlay-Fläche pro Monitor im Frontend.
