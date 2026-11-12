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
KWin-Script ──Update(json)──▶ ┌──────────────────────────────┐ ──State──▶ Bedien-Anwendungen (§10)
(Fenster, Stacking,           │ deckery-atspi (1 Prozess,    │ ──State──▶ Menü-Badges
 Monitore, aktives Fenster)   │ 1 Thread, 1 GLib-MainLoop)   │
                              │                              │
AT-SPI-Events ──────────────▶ │ Szene → Sichtbarkeit →       │
                              │ Elemente → Veröffentlichen   │
                              └──────────────────────────────┘
```

- **Ein Prozess, ein Thread.** Alles AT-SPI-bezogene läuft im Default-MainContext
  dieses Prozesses. Keine Locks. Einzige Ausnahme vom Default-Context: das
  gebündelte Einlesen (`atspi_bus.py`) hat eine eigene
  `Gio.DBusConnection` zum AT-SPI-Bus mit eigenem `GLib.MainContext`, der
  nur während eines Lesevorgangs als thread-default gesetzt und iteriert
  wird — der Default-Context (Events, KWin) läuft dabei nie verschachtelt.
  GDBus' interner Worker-Thread existiert ohnehin schon (Session-Bus).
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
- **Gebündeltes Einlesen (2026-11-12):** Qt arbeitet parallel gestellte
  Anfragen zwar der Reihe nach ab, aber ohne Rundlauf-Wartezeit dazwischen:
  200 gleichzeitige `GetRole` an Dolphin ≈ 0,09 ms pro Aufruf statt
  0,4–0,7 ms einzeln. `reader.fetch_tree` holt pro Knoten `GetRole`,
  `GetState`, `GetChildren` gleichzeitig (Kinderliste spekulativ), danach
  `GetExtents`/`Name` nur, wo der Walk sie braucht; alle Knoten einer Front
  sind gleichzeitig unterwegs. Der Walk selbst ist derselbe für beide
  Quellen (`select_targets`), die Abstiegsregeln teilen sich Walk und
  Fetch → Ausgabe identisch (verglichen mit dem bisherigen Reader auf
  Dolphin inkl. Kontext- und Untermenüs, System Settings, Panel,
  Kalender-Flyout, GTK4/zenity: gleiche Elemente, Clips, Web-Flags und
  Scroll-Proben; Abweichungen nur, wo sich der Baum zwischen beiden
  Lesevorgängen live änderte). `DECKERY_READER=sync` = alter Weg,
  `=compare` liest beides und loggt Unterschiede. Gepaarte Mediane
  (beide Reader direkt nacheinander auf demselben Fenster; Testrechner
  unter Fremdlast, Load 15–25 — absolute Werte daher höher als oben):

  | Fenster | Knoten | Aufrufe | libatspi | gebündelt | Faktor |
  |---|---|---|---|---|---|
  | Dolphin | 113 | 470 | 550 ms | 133 ms | 4,1× |
  | System Settings | 137 | 490 | 366 ms | 94 ms | 3,9× |
  | Kalender-Flyout | 362 | 1180 | 1410 ms | 226 ms | 6,3× |
  | Panel | 40 | 162 | 198 ms | 42 ms | 4,8× |
  | erstes Einlesen ganzer Bildschirm (Summe, 5 Starts) | — | — | 1515 ms | 434 ms | 3,5× |

  Das ganze erste Update (inkl. Frame-Liste, Zuordnung) im reinen Modus:
  Median 1,6 s → 0,7 s. Jetzt CPU-gebunden im eigenen Prozess (~70–100 µs
  pro Aufruf in GDBus + PyGObject, Python-Rest gering). Auch der
  Freshness-Poll des aktiven Fensters fragt seine ≤ 60 Rechtecke gebündelt.
  Nächste Hebel: `list_frames` (noch libatspi, 60–500 ms beim Start),
  Rollen pro Objekt-ID cachen (−25 % Aufrufe bei erneutem Lesen; Risiko
  wiederverwendeter Pfade). Chromium (Flatpak-Proxy) damit noch nicht
  gemessen.
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
- **Chromium mit dem gebündelten Reader:** ~255 Knoten in 150–280 ms statt
  1–1,5 s. Dadurch neu sichtbar: beim Scrollen einer Liste aktualisiert
  Chromium erst die Positionen, SHOWING etwas später — ein schnelles Lesen
  dazwischen behält weggescrollte Zeilen als Ziele, und der Poll vergleicht
  nur Rechtecke. → Nachlese-Durchgang auch nach poll-ausgelöstem Lesen.
- Electron: noch keine App installiert, ungetestet.
- Sichtbare Splitter: ein Fenster, das 3 px unter einem anderen hervorschaut,
  liefert 3 px breite Ziele — korrekt nach Scope, für die Navigation
  vermutlich nutzlos (Mindestgröße? offen).
- **Firefox 152 (Flatpak, 2026-11-12)**, getestet mit Wegwerf-Profil
  (`tools/testdesk/firefox.sh`), Fehlerbilder vom echten Desktop:
  - Werkzeugleisten-Knöpfe (Reload, App-Menü, Erweiterungen …) sind nie
    `focusable` → `button`/`toggle button` zählen auch ohne Fokus.
    Deaktivierte (Zurück ohne Verlauf) haben weder ENABLED noch SENSITIVE.
  - Die automatisch versteckte Menüleiste: Datei/Bearbeiten/… haben SHOWING
    genau dann, wenn sie sichtbar ist (alte Prototyp-Regel „benannte Menüs
    immer“ entfernt — die Geister-Umrisse über der Tableiste); ihre
    Werkzeugleiste meldet (-1,-1,-1,-1), der Schließen-Knopf darin bleibt
    SHOWING oberhalb des Fensters → Werkzeugleiste ohne Größe wird nicht
    betreten.
  - App-Menü (`panel`) und Kontextmenüs (`menu`) sind **Kinder des
    Browser-Frames** in dessen Koordinaten, KWin zeigt sie als eigene
    Popup-Fenster. Zuordnung über Lage + Größe (Toleranz 4 px: Firefox
    rundet CSS-Pixel, KWin ist logisch bei Skalierung 1,1), eingelesen als
    eigener Frame mit Rebase auf den Popup-Ursprung.
  - Ein **geschlossenes** Kontextmenü bleibt mit seinen Einträgen im Baum,
    die Einträge sogar SHOWING; nur das Menü selbst ist weder SHOWING noch
    VISIBLE → solche Knoten werden in keinem Toolkit betreten.
  - Fenster, Seitenrahmen (`internal frame`) und Popup-Wurzel sind nie Ziele.
- **Hängende AT-SPI-Anwendungen (echter Desktop):** `ibus-extension-gtk3`
  (~20 s) und ein Python-Script mit blockierter Hauptschleife (~6 s) pro
  Fensterliste — das Backend stand so lange still (2–4 s nachhängende
  Umrisse). Jetzt: alle Anwendungen parallel mit 500 ms anfragen, stumme
  30 s auslassen und mit ihren bisherigen Frames behalten.
- **KWins eigenes Fenstermenü** (Fenster-Icon, Alt+F3): ein KWin-internes
  Fenster ohne AT-SPI-Inhalt — die KWin-Anwendung hat keine Kinder und
  sendet keine Events. Nicht lesbar.
- Nicht getestet: Tastaturfokus.
- KWin-Scripts: `frameGeometry` lässt sich nur aus QML-Scripts setzen
  (`Qt.rect`); einfache Scripts ignorieren die Zuweisung stillschweigend.

## 10. Konsumenten (entschieden 2026-11-12)

Backend, Eingabe und Bedienung sind getrennt:

- **Backend (dieses Projekt):** veröffentlicht, was sichtbar und erreichbar
  ist — vollständige Zielliste per `Changed`, sonst nichts. Kein
  Positions-Lookup, keine Auswahl, kein Zustand der Bedienung: der Prozess
  ist während eines Einlesens (40–280 ms, mit libatspi bis > 1 s) blockiert
  und gehört nicht in einen Pfad mit Pad-/Stick-Rate.
- **Makima (Rust):** liefert Eingaben (Pad-Position, Klick, Stick) und spielt
  Haptik ab.
- **Bedien-Anwendungen** — eigene Clients von Makima und Backend, z.B.
  Sprung-Navigation (relativ, Zeiger springt zwischen Elementen) und
  Tastkarte (absolut, Pad-Position → nächstes Element): halten die
  Zielliste lokal, entscheiden selbst über Auswahl und Hysterese, bewegen
  den Zeiger, fordern Haptik bei Makima an. Welche Modi und wie sie sich
  bedienen ist offen und unabhängig vom Backend.

## 11. Offene Fragen

1. Bedienmodi (s. §10): Details offen, eigenes Thema.
2. Mehrere Monitore: eine Overlay-Fläche pro Monitor im Frontend.
3. Mindestgröße sichtbarer Teile (3-px-Splitter, §9) — Backend oder
   Bedien-Anwendung?
