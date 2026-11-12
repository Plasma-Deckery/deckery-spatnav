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
- Tastaturfokus: gemessen, siehe §12.
- **GTK4-Testanwendung (2026-11-12)**, `tools/testdesk/gtk_testapp.py`
  (GTK 4.22, Szenarien `gtk_*`) — alle gängigen Steuerelemente mit festen
  Namen, deaktivierte, versteckte, drei Scroll-Container, Popover, Dialog:
  - **Deaktivierte Container:** GTK4 meldet SENSITIVE pro Widget, nicht
    geerbt — der Knopf in einem deaktivierten `Gtk.Frame` ist SENSITIVE,
    nur die `grouping` des Frames nicht. Für GTK gilt jetzt: unter einem
    Knoten ohne ENABLED/SENSITIVE ist alles deaktiviert
    (`INHERIT_DISABLED_TOOLKITS`; andere Toolkits nicht geprüft, daher
    nicht übertragen). Nebenbei: GTK macht die Frame-Beschriftung zum
    Namen des Knopfes darin.
  - **DropDown:** nicht fokussierbare `combo box` um einen `toggle button`
    um ein fokussierbares, unbenanntes `panel` → vorher zwei Ziele (eins
    unbenannt). Jetzt: die combo box ist Ziel (auch ohne FOCUSABLE),
    was in ihrem eigenen Rechteck liegt, wird nicht betreten — nur ihre
    Liste außerhalb.
  - **Gleicher Name, andere Rolle:** MenuButton = nicht fokussierbarer
    `button` um fokussierbaren `toggle button` (gleiches Rechteck);
    Baumzeile = `list item` um den `button` ihres TreeExpanders (4 px
    kleiner). Jetzt: ein gleichnamiges Ziel im Ziel zählt einmal — das
    fokussierbare, sonst das äußere. (Eine Webseiten-`li` um einen
    gleichnamigen Link behält so den Link.)
  - **Popover** (MenuButton-Menü, Popover, DropDown-Liste) sind für KWin
    eigene Popup-Fenster, für AT-SPI aber ein Kind des aufgeklappten
    (EXPANDED) Widgets tief im Baum des Hauptfensters, in dessen
    Koordinaten (inkl. ~3 px Schattenrand) und ohne
    `state-changed:showing`. Vorher: Popup-Fenster nicht zugeordnet, die
    Einträge im Hauptfenster vom Popup verdeckt → keine Ziele, nur ein
    Umriss im Schattenrand. Jetzt markiert der Reader Elemente im Popup
    eines EXPANDED-Knotens (`RawElement.popup`: Kind außerhalb des
    Knotens, bzw. Liste einer combo box), und das Backend zeigt die
    Gruppe im Popup-Fenster, in dem alle ihre Elemente liegen
    (`_match_popovers`), platziert wie das Hauptfenster — ohne zweites
    Einlesen. Popup offen → Einträge Ziel nach 0,5–0,9 s.
  - **GTK4-Menüeinträge haben keinen Namen** (PopoverMenu: `labelled-by`
    zeigt auf ein ungültiges Objekt, kein Label-Kind). DropDown-Einträge
    ebenso, der Text steckt nur im Label-Kind. Nicht behoben (das
    Backend liest Namen nur von Zielen).
  - Modaler Dialog (`Gtk.Window`, modal, transient): eigenes normales
    KWin-Fenster, eigener AT-SPI-Frame — funktionierte ohne Änderung.
    Die Ziele des Hauptfensters neben dem Dialog bleiben Ziele (sichtbar,
    GTK ignoriert Klicks darauf) — Sache der Bedien-Anwendung (§12).
  - Scrollen (Viewport mit gemischten Widgets, ListBox, ListView-Baum mit
    recycelten Zeilen) und Auf-/Zuklappen im Baum: funktionierte ohne
    Änderung, Ziele folgen in ~1 s.
  - GTK-Popups einer per `python3` gestarteten App haben in KWin die
    Klasse `python3.14` — wie der Viewer. `ctl.check` erkennt den Viewer
    jetzt an seiner PID-Datei, nicht an der Klasse (blendete sonst die
    Popups aus der Verdeckungsprüfung aus).
- **Chromium `<select>` offen (2026-11-12):** eigenes KWin-Popup-Fenster
  mit leerem eigenem AT-SPI-Frame (Flatpak: nicht zuordenbar), die
  Optionen stehen im Hauptbaum unter der combo box (ihr `menu` meldet ein
  falsches 72×11-Rechteck *in* der combo box) in Seitenkoordinaten.
  Einziges Event: `state-changed:expanded`. Vorher keine Ziele; jetzt
  über denselben Weg wie GTK-Popover (expanded ist neu ein Inhalts-Event),
  Optionen nach ~0,4–0,7 s Ziele des Popup-Fensters. Esc schloss die
  Liste im Testdesktop nicht.
- **Firefox zeichnet Popups teils im eigenen Fenster:** die offene
  `<select>`-Liste und die Vorschlagsliste der Adressleiste sind keine
  KWin-Fenster. Die Optionen werden Ziele, aber Seiten-Ziele **unter**
  der Liste bleiben ebenfalls Ziele — Verdeckung innerhalb eines
  Fensters kennt das Backend nicht (wie bei QML-Popups). Offen.
  Die Vorschlagsliste öffnet sich beim Start mit fokussierter
  Adressleiste und schluckt Klicks auf die Seite → im Wegwerf-Profil
  abgeschaltet (`firefox.sh`). Firefox' Adressleiste ist eine
  `combo box`.
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

## 12. Messung: Signale für Sondermodi (2026-11-12, nur gemessen)

Frage (nicht entschieden): Bedien-Anwendungen müssen sofort wissen, ob
ein Menü/Popup oder ein modaler Dialog offen ist, ein Textfeld den Fokus
hat oder ein Regler/Zahlenfeld/eine Auswahlliste gewählt ist — getrennt
vom langsamen Einlesen der Zielliste. Gemessen mit
`tools/testdesk/mode_probe.py`: echte Eingabe über EIS
(`eis_input.py` stempelt den Druck mit CLOCK_MONOTONIC, im Container
dieselbe Uhr), Ankunft je Quelle in ms **nach dem Drücken** (Klick-
Loslassen +80 ms, Taste +60 ms). Quellen: KWin-Script
(`windowAdded/Removed/Activated` mit popup/modal/transient), AT-SPI
(`state-changed:focused` gewonnen, `:expanded`, `:showing` popup-artiger
Rollen, `window:activate`), Backend-`Changed`. Testdesktop unter
Fremdlast; Spannen über zwei Läufe, erste Berührung einer App teils
deutlich langsamer (Dolphin-Kontextmenü erstmals 650–750 ms).

| Situation | Signal | GTK 4.22 (Testapp) | Qt Widgets (Dolphin) | Qt Quick (Systemeinst.) | Chromium 156 | Firefox 152 |
|---|---|---|---|---|---|---|
| Textfeld | focus + EDITABLE | ja, 2–6 ms (`text`/`password text`, MULTI_LINE) | ja, 16–20 ms (Suche); Adresszeile: erst `check box`, dann editierbare `combo box` 25 ms | Suchfeld: **kein Event** (Start-Fokus, Strg+F) | ja, 3–21 ms (`entry`, SINGLE/MULTI_LINE) | ja, 7–10 ms (`entry`) |
| Regler | focus + Value | ja, 6–10 ms | – (keiner) | ja, 14 ms (nach `filler`-Fokus) | ja, 4–7 ms | ja, 6 ms |
| Zahlenfeld | focus + Value | ja, 3–46 ms (`spin button`, EDITABLE) | – | **Fokus aufs innere `text`**, ohne Value, 83 ms | ja, 7–17 ms | ja, 6 ms |
| Auswahlliste auf | expanded | combo box, 145–166 ms | – | – (keine) | 22–33 ms | 53 ms |
|  | KWin-Popup | 180–230 ms | – | – | 110–160 ms (Klasse leer) | **keins** (im Fenster) |
|  | focus in Liste | `list item` (unbenannt) | – | – | `menu item` 80–94 ms | `menu item` 36 ms |
|  | Backend: Optionen Ziele | 650–890 ms | – | – | 390 ms | 280 ms (im Hauptfenster) |
| Menü/Popover auf | expanded | äußerer `button`, 118–408 ms | – (keins) | – | – | – |
|  | showing `popup menu` | – (keins) | 10–90 ms | 89 ms | – | – |
|  | KWin-Popup | 130–460 ms | 32–95 ms | 133 ms | – | – |
|  | Backend: Einträge Ziele | 525–1400 ms | 59–195 ms | 184 ms | – | – |
| Modaler Dialog auf | KWin added+activated, `modal` | 121–299 ms, modal ✓ | 124 ms, modal ✓ | – | **nichts** (`<dialog>.showModal()`) | **nichts** |
|  | AT-SPI | `window:activate` des Dialog-Frames mit MODAL, 143–345 ms | nur Fokus ins Feld, 139 ms | – | nur Fokus ins Feld, 100 ms | nur Fokus ins Feld, 88 ms |
|  | Backend: Dialog-Ziele | 261–368 ms | 209 ms | – | 296 ms | 292 ms |
| Popup/Dialog zu | KWin removed | 8–125 ms | 6–35 ms | 9 ms | 95 ms | – |
|  | expanded=0 / showing=0 | 5–14 ms | 4–15 ms | 7 ms | 115 ms | 86 ms (`window` showing=0) |
| Tab-Taste | focus | 13–16 ms | – | – | **kein Event** (2×) | 22 ms |

Immer sofort (0 ms, lokal): die **Rolle des gewählten Ziels** aus der
Zielliste — `text`/`entry`/`password text`, `slider`, `spin button`,
`combo box`; Menüknöpfe sind uneinheitlich (`toggle button` in GTK,
`push button menu` in Qt, `button` „Firefox"); HAS_POPUP stünde nur in
den Zuständen, die das Backend nicht veröffentlicht. Bei Qt Quick ist die
Rolle der einzige Hinweis aufs Zahlenfeld (Fokus landet aufs Kind).

Zuverlässigkeit der Zustände:
- **EDITABLE**: alle fünf bei Textfeldern; Interfaces taugen nicht als
  Ersatz (Chromium: kein EditableText; Firefox: EditableText auch an
  Knöpfen).
- **FOCUSED**: kommt überall als Event, aber oft als Kette — Qt Quick
  fokussiert erst einen `filler` (FocusScope), Dolphin bei Strg+L erst
  einen `check box`; der letzte Fokus eines Schubs (~25 ms) zählt.
  Lücken: Systemeinstellungen-Suchfeld, Chromium bei Tab.
- **EXPANDED**: GTK (äußerer Knopf bzw. combo box), Chromium und Firefox
  (combo box); Qt nie — dort zeigt `showing` der `popup menu` das Menü an.
- **Value** (min/aktuell/max): GTK, Chromium, Firefox, Qt Quick am
  Regler; nicht am fokussierten Kind eines Qt-Quick-Zahlenfelds.
- **MODAL**: am GTK-Dialog-Frame und als KWin-`modal` (GTK, Qt); Web-
  Dialoge (`showModal`) liefern gar kein Fenster-Signal.
- Das Nachfragen von Rolle/Zuständen/Value pro Event kostet synchron
  2–150 ms (erste Berührung einer App bis 400 ms) — ein Konsument muss
  das asynchron/gebündelt tun oder sich auf die Event-Rolle beschränken.

Lesart: Fokus-Events sind mit 2–20 ms die schnellste Quelle für
Text/Regler/Zahlenfeld; KWin-Fenster für Popups/Dialoge (30–300 ms) sind
toolkit-unabhängig, decken aber Firefox-Listen und Web-Dialoge nicht ab;
die Backend-Zielliste folgt 0,2–1,4 s später. Rohdaten:
`mode_probe.py … --json`.
