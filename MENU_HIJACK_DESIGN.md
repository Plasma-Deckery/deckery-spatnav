# Menü-Hijacking — Design-Brainstorm (Ticket)

**Status:** Brainstorm, keine Entscheidung final. Dient als Referenz für zukünftige
Sessions, wenn der Konversationskontext verloren gegangen ist. Ergänzt
[CURSOR_NAV_DESIGN.md](CURSOR_NAV_DESIGN.md) (Stick-Navigation zwischen Elementen) um
einen zweiten, unabhängigen Navigationsmodus speziell für Kontextmenüs/Popup-Menüs.

---

## 1. Ziel

Wenn ein Menü (Kontextmenü, Dropdown, App-Menü) geöffnet ist, soll neben jedem
Menüeintrag ein kleines Badge erscheinen, das die Controller-Eingabe zeigt, mit der
dieser Eintrag **direkt** ausgewählt werden kann — ohne durch das Menü zu navigieren.
Beispiel: oberster Eintrag → A, zweiter → X, dritter → B, usw. Perspektivisch als
neuer Modus in Makima.

Kernidee des Nutzers: AT-SPI-Events nutzen, um zu erkennen, wann ein Menü
geöffnet/fokussiert ist bzw. wieder verschwindet, und daraus einen "Hijack"-Modus zu
bauen, der die Eingaben umleitet, solange das Menü offen ist.

---

## 2. Warum das (potenziell) geräteunabhängiger ist als der Rest des Projekts

Der bisherige Code (siehe [REQUIREMENTS.md](REQUIREMENTS.md)) hängt für zwei Dinge hart
an KWin:
- Virtuelle-Desktop-Filterung (`deckery_tracker.py` + KWin-Script)
- Bildschirm-Offset-Auflösung für Fenster (CSD-Ränder, Scale-Korrektur)

Die Menü-Erkennung selbst braucht **keins von beidem** — sie basiert rein auf
AT-SPI2-Events (`object:state-changed:showing`, `window:create`/`destroy` für Popups),
und AT-SPI2 ist ein Desktop-Standard, den GNOME genauso implementiert wie KDE. Das ist
der eigentliche Reiz der Idee: potenziell der erste Teil des Projekts, der nicht an
KWin hängt.

**Offene Frage, die das relativiert:** Für die Bildschirmposition der Badges brauchen
wir trotzdem die Extents des Menüs auf dem Screen. Bei normalen Fenstern brauchten wir
dafür das KWin-Script (bufferGeometry/clientGeometry, siehe REQUIREMENTS.md §9). Bei
Popup-Menüs ist unklar, ob `Atspi.CoordType.SCREEN` bereits korrekte
Bildschirmkoordinaten liefert (Popups werden oft direkt vom Compositor positioniert,
anders als CSD-Fenster) — **muss empirisch geprüft werden, bevor wir annehmen, dass wir
ohne KWin auskommen.**

---

## 3. Bekannte Zuverlässigkeitsrisiken (aus dieser Session, direkt übertragbar)

Die "Menü ist gerade offen / ist gerade zu"-Erkennung ist **kein sauberes
Binärsignal** — das haben wir diese Session mehrfach schmerzhaft gelernt:

- **Firefox**: Menüleisten-Einträge (File/Edit/View) melden `showing=False` auch wenn
  die Leiste sichtbar ist (Gecko-Eigenheit, siehe REQUIREMENTS.md §4.1) — Namens-Check
  als Sonderfall nötig.
- **Firefox, echte Dropdown-Inhalte**: einmal geöffnete Untermenüs (History,
  Bookmarks) scheinen ihren SHOWING-Status nicht zuverlässig zurückzusetzen, wenn sie
  schließen — bestätigt live diese Session (100+ Geister-Menüeinträge nach mehrfachem
  Öffnen/Schließen).
- **plasmashell/KDE-Popups**: State-Flags "kleben fest" — manche meckern permanent
  `SHOWING=False` obwohl offen, andere permanent `True` obwohl zu (REQUIREMENTS.md
  §13.1). Fallback dort: `frame_has_valid_extents()` statt State-Trust.
- **Event-Filter-Falle**: Unsere eigene `EventSpamFilter`-Allowlist filterte
  `state-changed:showing` für die Rolle `MENU_ITEM` lange raus (Orcas
  Sprachausgabe-Logik übernommen, wo das Sinn ergab) — für einen visuellen Zweck wie
  diesen ist das aber genau der Fehler, der Geister-Eintrge verursacht. **Bereits
  gefixt** in `deckery_event_filter.py` (MENU_ITEM/CHECK_MENU_ITEM/RADIO_MENU_ITEM
  ergänzt) — gilt automatisch auch für dieses Feature, wenn es dieselbe Cache-Pipeline
  nutzt.

**Konsequenz für den Prototyp:** "Menü ist offen" sollte nicht auf einem einzelnen
Signal beruhen, sondern auf mehreren, mit App-spezifischen Sonderfällen wo nötig — exakt
das Muster, das `prototype_atspi_scan.is_relevant_element()` schon für normale
Elemente fährt.

---

## 4. Electron/Chromium — differenzierter als der Rest des Projekts

[ATSPI_LIMITATIONS.md](ATSPI_LIMITATIONS.md) dokumentiert: Electron-Hauptfenster
exponieren keine Kind-Elemente über AT-SPI (nur der Fensterrahmen). **Das gilt aber
möglicherweise nicht für native Kontextmenüs** — Electrons `Menu`/`MenuItem`-API
rendert auf Linux oft über die native GTK-Menü-Implementierung, nicht über Chromiums
Web-Rendering-Pfad. GTK-Menüs haben eingebaute AT-SPI-Unterstützung, unabhängig davon,
ob die Web-Content-Accessibility der App aktiviert ist. **Unbestätigt, muss getestet
werden** — wäre aber ein Fall, wo dieses Feature in Electron-Apps funktioniert, obwohl
der Rest der Overlay-Erkennung dort blind bleibt.

---

## 5. Adressierungsschema (Nutzer-Entscheidung, diese Session)

Nicht nur die 4 Face-Buttons (A/B/X/Y) — das reicht bei Weitem nicht (Firefox
History-Menü hatte diese Session 90+ Einträge). Stattdessen:

- **D-Pad (links) × Face-Buttons (rechts)** als 2D-Adressraum — bis zu 4×4 = 16 direkt
  adressierbare Einträge ohne Modifier.
- **L1/L2/R1/R2 als Modifier** (wie Shift-Tasten) zur weiteren Vervielfachung des
  Adressraums für noch längere Menüs.
- Reihenfolge/Zuordnung (welcher Eintrag bekommt welche Kombination) noch offen —
  vermutlich Lesereihenfolge (oben→unten, dann links→rechts bei mehrspaltigen Menüs).

**Konsequenz fürs Badge-Design:** Nicht ein einzelnes Tasten-Icon pro Eintrag, sondern
potenziell eine Kombi-Anzeige (z.B. D-Pad-Richtung + Face-Button-Icon, oder ein
Modifier-Icon + Basis-Taste). Visuelles Design noch offen.

---

## 6. Scope für diese Prototyping-Phase

**Nur KDE/Wayland** (Nutzer-Entscheidung) — wie der Rest des Projekts. GNOME/X11-Test
erst, wenn der Kern auf KDE funktioniert. Die AT-SPI-Ebene selbst *sollte*
geräteunabhängig sein, aber das wird nicht in dieser Phase verifiziert.

---

## 7. Klargestellt: kein Kando-Nachbau, reines Overlay

Nach kurzer Abwägung (Session vom 2026-09-28): **kein** Nachbau des echten Menüs
(Kando-Stil) — bringt bei langen Menüs keinen klaren Vorteil und zieht drei Dinge nach
sich, die technisch nicht sauber gehen:

- **Eigenen Inhalt ins echte Menü injizieren** (z.B. per Label) — AT-SPI ist eine
  Beobachtungs-/Auslöse-Schnittstelle, kein Weg, einer fremden App Widgets in ihren
  UI-Baum zu schieben. Nicht möglich.
- **Schließverhalten des echten Menüs übersteuern** (z.B. Menü offen lassen bei
  Checkbox/Radio-Klick) — entscheidet die Ziel-App/das Toolkit intern, AT-SPI gibt uns
  dafür keinen Hebel. Nicht von außen erzwingbar; manche Toolkits lassen solche
  Einträge von sich aus offen, das ist aber nicht einheitlich und nicht durch uns
  steuerbar.
- Das echte Menü optisch verstecken, um durch ein eigenes zu ersetzen — theoretisch
  über KWin-Fenstermanipulation machbar, aber unnötiger Aufwand, wenn wir eh nur
  daneben zeichnen wollen.

**Stattdessen: reines Overlay-Prinzip**, exakt wie der Rest des Projekts — wir lesen
Position/Struktur des echten Menüs (inkl. Untermenüs) und zeichnen unsere Badges
**daneben**, ohne das echte Menü anzufassen oder zu verändern.

---

## 8. Architektur: eigener Toggle-Modus (wie cursor_nav_tray.py)

Kein permanent aktives Feature, sondern ein **eigener Modus**, der über einen
Tray-Toggle an/aus geschaltet wird (gleiches Muster wie der bestehende
Cursor-Nav-Toggle) — solange aus, lauscht nichts, keine Kosten. Solange an: AT-SPI-
Event-Listener für Menü-relevante Events aktiv (state-changed:showing für
MENU/POPUP_MENU/MENU_ITEM-Rollen, s.o.).

**Wichtige Anforderung, explizit genannt:** Beim Öffnen/Schließen von Untermenüs muss
eindeutig klar sein, **welches** Untermenü betroffen ist — nicht nur "irgendein Menü hat
sich geändert". Braucht wahrscheinlich Objekt-Identität (hash(accessible), wie
`ElementCache` es schon macht) pro Submenü-Ebene, nicht nur einen globalen
offen/zu-Flag.

---

## 9. Zeichen-Engine: erster, bewusst simpler Entwurf

Kein Versuch, den (nicht zuverlässig vorhandenen) Tastaturkürzel-Platz rechts im
Menüeintrag wiederzuverwenden — manche Einträge haben dort Platz reserviert, andere
nicht, kein verlässliches Muster. Stattdessen:

- Badge ist **quadratisch**, Seitenlänge ≈ Höhe des Menüeintrags (aus dessen eigenem
  Rect), minus etwas Padding.
- Position: **direkt links** neben dem Eintrag-Rect (`item.x - badge_width - padding,
  item.y, badge_width, item.height`).
- Gilt einheitlich für jeden Eintrag in jedem offenen Menü/Untermenü — keine
  Sonderfälle für "hat Shortcut-Platz" vs. "hat keinen".

Einfachster Ansatz, der zuverlässig funktioniert, statt eine optische Lücke zu suchen,
die nicht immer da ist.

---

## 10. Prototyp-Befunde (Session vom 2026-09-28, `menu_badge_prototype.py`)

Erster Prototyp gebaut: Event-Listener + Layer-Shell-Overlay, zeichnet Quadrate links
neben jedem Menüeintrag. Ergebnisse aus echten Tests:

- **Plasma-Tray-Kontextmenü**: funktioniert einwandfrei, Position korrekt.
- **Dolphin-Kontextmenü**: Position der sichtbaren Einträge korrekt, aber manche
  Einträge (KIO-Service-Menu-Plugins) fehlen **komplett im AT-SPI-Baum** — bestätigt
  per Tree-Dump (180px-Lücke zwischen zwei Separatoren ohne jedes Kind-Element dazwischen).
  Gleiche Kategorie wie die dokumentierte Electron-Lücke (ATSPI_LIMITATIONS.md) — von
  außen nicht behebbar. **Recherchiert (2026-09-28)**: KDE/Dolphin-Barrierefreiheit für
  den echten Screenreader Orca hat unabhängig bestätigte, dokumentierte Lücken
  (Kontextmenü-Bedienung, Properties-Dialog nicht vollständig zugänglich,
  Navigations-Lags — mehrere offene KDE-Bugzilla-Einträge). Kein exakter Bug-Report zu
  "KIO-Service-Menu-Einträge fehlen im AT-SPI-Baum" spezifisch gefunden, aber der
  generelle Befund (Dolphins Kontextmenü-Accessibility ist lückenhaft) ist unabhängig
  bestätigt — wir sind nicht die Ersten, die hier auf Lücken stoßen.
- **Window Menu** (Icon oben links am Fenster, App `kwin`): Position ist **korrekt**
  (`rect=(11,59,307,193)`, kein 0,0-Bug) — aber das Menü wird **über** unserem
  Overlay gezeichnet, obwohl wir `Gtk4LayerShell.Layer.OVERLAY` nutzen (die oberste
  Layer-Shell-Ebene). **Recherchiert und bestätigt** (2026-09-28,
  [swaywm/sway#4684](https://github.com/swaywm/sway/issues/4684)): "xdg-shell popups
  are already drawn on top of other toplevels, regardless of the toplevels z-index" —
  eine **Wayland-Protokoll-Konvention**, kein KWin-spezifischer Bug. `xdg_popup`-
  Surfaces (das Window Menu ist genau das) bekommen compositor-übergreifend
  bevorzugte Stacking-Behandlung, unabhängig vom Layer-Shell-Layer. Es gibt keine
  "höhere Ebene" zu wählen — das Problem ist strukturell auf Protokollebene, nicht von
  außen fixbar außer über den invasiveren Weg aus §7 (echtes Popup verstecken/ersetzen).
- **"Global Menu"-Panel-Dropdown** (File/Edit/View/... als ein Panel-Button):
  Position ist **falsch** — meldet `rect=(0,0,96,218)`, buchstäblich Bildschirm-Ursprung,
  unabhängig von der echten Panel-Button-Position. Bestätigt per Tree-Dump. Läuft über
  KDEs DBusMenu/Appmenu-Bridge (App zeigt sich als `kded6`, nicht die eigentliche App) —
  ein anderer, dünnerer Mechanismus als normale In-App-QMenüs, mit eigenem (kaputtem)
  Koordinaten-Reporting. Tiefere Untermenüs innerhalb dieses Dropdowns melden dagegen
  plausible Werte relativ zu diesem falschen Ursprung.
  **Recherchiert (2026-09-28)**: Direkt verwandt mit einer fundamentalen
  Wayland-Einschränkung, bestätigt in
  [isac322/kwin-mcp#51](https://github.com/isac322/kwin-mcp/issues/51): "On Wayland,
  clients cannot know their own window position" — Wayland-Clients kennen ihre
  absolute Bildschirmposition grundsätzlich nicht (Sicherheitsdesign, kein Versehen).
  Der dort vorgeschlagene Fix ist exakt unser eigener Ansatz (KWin-Toplevel-Geometrie
  + Offset). Verknüpft sich direkt mit der **schon in REQUIREMENTS.md §18.2
  dokumentierten Lücke**: transiente Popups tauchen gar nicht in
  `workspace.windowList()` auf — die Quelle unserer Offsets. Kein neues Problem,
  sondern eine bestätigte Instanz einer bereits bekannten, bewusst nicht umgesetzten
  Lücke. Weitere Bestätigung, dass wir nicht die Ersten sind:
  [KDE Bug 436016](https://bugs.kde.org/show_bug.cgi?id=436016) "[Wayland] Context
  Menu Position Wrong", [KDE Bug 447330](https://bugs.kde.org/show_bug.cgi?id=447330)
  "Context menus have wrong position on multi-monitor setups". Möglicher Workaround
  (nicht umgesetzt): KWin-Script um Tracking transienter Popups erweitern (genau der
  in §18.2 skizzierte, für die MVP bewusst ausgelassene Weg), oder Position des
  auslösenden Panel-Buttons statt der Menü-eigenen Extents nutzen.
- **Schließen-Erkennung unzuverlässig, wiederholt bestätigt** (Tray, Dolphin, Global
  Menu): `state-changed:showing` mit `detail1=0` feuert bei manchen Popups einfach
  nie — exakt die schon dokumentierte plasmashell-"Stuck State"-Eigenheit
  (REQUIREMENTS.md §13.1), hier auch bei DBusMenu-Popups beobachtet. **Gefixt**: Bei
  jedem Refresh wird der Live-SHOWING-Status jedes getrackten Menüs zusätzlich
  nachgeprüft (nicht nur dem Event vertraut), plus ein 1s-Sicherheits-Timer, falls gar
  kein anderes Event mehr feuert, das einen Refresh auslösen würde.

---

## 11. Offene Fragen für die Umsetzung

1. Braucht die Badge-Positionierung bei Popup-Menüs wirklich den KWin-Offset-Tanz, oder
   liefert `CoordType.SCREEN` bei Popups bereits korrekte Koordinaten? (§2)
2. Welche AT-SPI-Rollen zählen als "Menü, das wir badgen" —
   `POPUP_MENU`, `MENU`, beides? Wie wird ein echtes Kontextmenü von einem Dropdown in
   einer normalen Toolbar unterschieden (falls nötig)?
3. Wie wird "Menü ist wirklich zu" robust erkannt, gegeben die dokumentierten
   Zuverlässigkeitsrisiken (§3) — reicht ein einzelnes Event, oder braucht es einen
   Timeout/Cross-Check wie bei plasmashell?
4. Zuordnungsreihenfolge der D-Pad×Face-Button-Kombinationen zu Menüeinträgen.
5. Visuelles Design der Kombi-Badges (Inhalt des Quadrats aus §9 — welches Icon/Text).
6. Wie wird das vom bestehenden `ElementCache`/`render_tree()`-Pfad getrennt oder darauf
   aufgebaut — eigener, schlanker Erkennungspfad oder Erweiterung der bestehenden
   Pipeline?
7. Wie interagiert dieser Modus mit dem Cursor-Nav-Modus aus
   [CURSOR_NAV_DESIGN.md](CURSOR_NAV_DESIGN.md) — exklusiv, oder beide gleichzeitig
   aktiv?

---

## 12. Nachtrag zu §10: Z-Order doch nicht unbedingt unfixbar

**Recherchiert (2026-10-02):** KWin-Scripting-Fensterobjekte haben ein
**`keepBelow`**-Property (lesbar/schreibbar) und ein `layer`-Property (0=Desktop bis
7=OverlayLayer). Es gibt ein dokumentiertes Praxisbeispiel für genau diese Art Problem
(Menü vs. Layer-Shell-Dock), gelöst durch explizite Layer-Zuweisung. Falls das Window
Menu als vollwertiges, scriptbares "Window"-Objekt auftaucht (nicht nur als reines
`xdg_popup` ohne Scripting-Zugriff — **noch nicht verifiziert**), könnte ein kleines
KWin-Script bei dessen Erscheinen `keepBelow = true` setzen. Noch nicht getestet, vom
Nutzer zurückgestellt ("Erstmal nur beobachten, nicht am Live-System testen").

## 13. Nachtrag: Trayscale (Drittanbieter-App) — AT-SPI-Exposition geprüft

Im Rahmen der "warum bekommen wir aus Trayscale keine Events"-Frage direkt am Baum
geprüft (2026-10-02): Trayscale (`dev.deedles.Trayscale`, Go + GTK4 via `gotk4`) **ist
nicht unsichtbar** für AT-SPI — der Baum hat echten Inhalt (1 Frame, 50 Panels, 16
Groupings, 1 Liste mit 6 Listenelementen, 6 Bildern, 7 Labels, 1 Scrollbar). Die
Listenelemente sollten über den bestehenden Fallback (Rolle `list item`) eigentlich
schon im Haupt-Overlay auftauchen — **Nutzer bestätigt aber: in Trayscale wurde noch
nie etwas eingezeichnet**, auch als `main.py` lief. Nicht abschließend geklärt, ob das
an (a) fehlenden Live-Update-Events (GTK4 ersetzte ATK/at-spi2-atk durch ein neues
`GtkAccessible`-Interface — Standard-Widgets bekommen automatisch Rollen, aber ob
`gotk4` die Change-Notification-Calls für dynamische Inhalte wie Geräte-Listen
korrekt durchreicht, ist offen) oder (b) einem anderen, noch nicht gefundenen Grund
liegt. **Noch zu tun**, sobald `main.py` wieder läuft: direkt prüfen, ob Trayscales
Listenelemente überhaupt in `ElementCache` landen (build_initial() sollte sie beim
ersten Walk schon erfassen, auch ohne jedes Live-Event).

---

## 14. Migrationspfad: Prototyp hier → Decory Makima (Session vom 2026-10-02)

Strategie des Nutzers: Features hier in diesem Nebenprojekt (`deckery-spatnav`) in
Python prototypen, dann Stück für Stück in **Decory Makima** (das eigentliche
Produktiv-Tool für Controller-Input-Bindings) übertragen. Das Menü-Hijacking-Feature
soll dort als **neue, eigene Datei** landen, die sich um Kontextmenüs kümmert — macht
Sinn, weil es im Kern um Input-Binding-Umschaltung geht (Makimas Kerndomäne), nicht nur
um AT-SPI-Erkennung. Nutzer ist sich bewusst, dass das hinterfragt werden sollte, falls
es nicht modular genug für Makima ist — aber geht erstmal davon aus, dass es passt.

## 15. Bindings-Override-Schema, solange ein Menü offen ist

Wenn ein Menü offen ist, werden **alle normalen Controller-Bindings deaktiviert**,
außer:
- Das auf **Escape** gemappte Binding (schließt Menüs) — bleibt aktiv.
- Das auf **Enter** gemappte Binding — bleibt ebenfalls aktiv (nicht für
  Badge-Shortcuts wiederverwendet).
- **Pfeiltasten/D-Pad-Navigation** — bleiben ebenfalls als normale Fallback-Navigation
  aktiv, werden nicht für Badge-Zuordnung zweckentfremdet.

Die **übrigen** Tasten (nicht viele) werden den Menüeinträgen zugeordnet — reichen
alleine nicht, müssen kombiniert werden (siehe §5: D-Pad×Face-Buttons,
L1/L2/R1/R2 als Modifier).

**Scoping bei Untermenüs:** Sobald ein Untermenü öffnet, zählen die Bindings des
äußeren Menüs nicht mehr — nur noch die des Untermenüs. Erfordert eine klare
Zustandsmaschine, welche Menü-Ebene gerade "aktiv" für Binding-Zwecke ist (hängt mit
der schon in §8 genannten Anforderung zusammen, Untermenüs eindeutig per Identität zu
tracken).

## 16. Gruppen-/Untermenü-Navigation — offene Spannung

Ideen des Nutzers, noch nicht entschieden:
- **L1 + Hoch/Runter** (oder **L1+R1**) zum Springen zwischen **Gruppen** (= durch
  Separatoren getrennte Abschnitte, wie in §10/Ticket schon als offene Frage #ehemals
  notiert).
- Alternative: L1+R1 springt direkt zu **allem, was aufklappbar ist** (Untermenüs).
- Alternative: Untermenü-Einträge bekommen einfach einen **ganz normalen
  Badge-Button**, wie jeder andere Eintrag auch — keine Sonderbehandlung nötig.
- **Deaktivierte Einträge** bekommen keinen Button zugewiesen (werden übersprungen).

**Offene, selbst gestellte Frage des Nutzers:** "Was ist der effektivste Pfad, um etwas
auszuwählen — sich durch L1/R1 durchklicken, oder direkt ein Binding pro Untermenü?"
Tendenz des Nutzers: lieber **ein direktes Binding pro Ziel** als mehrstufiges Paging —
aber explizit als "noch nicht im Detail entschieden, reicht erstmal für einen
Prototypen" markiert.

Die gewählte Strategie soll **konfigurierbar** sein (passt zum bestehenden Muster:
`cursor_nav_tray.py` hat bereits ein Strategie-Untermenü mit Live-Umschaltung via
`org.deckery.CursorNav`/`SetStrategy`).

## 17. Architektur-Trennung: Erkennung/Zeichnen/Konsument — und die Python-vs-Rust-Frage

Idee des Nutzers: das Ganze in drei Schichten trennen —
1. **Erkennung + Positionsberechnung** (unser AT-SPI-Backend, das, was wir diese
   Session gebaut haben) — liefert nur: "hier ist ein Menüeintrag, hier ist seine
   Position".
2. **Zeichnen** — könnte das bestehende **Decory HUD**
   (`~/.local/share/deckery/deckery-hud/`) übernehmen, statt dass jeder Prototyp sein
   eigenes Layer-Shell-Fenster zeichnet. Backend würde dann nur noch State
   hinterlegen ("hier malen"), nicht selbst rendern.
3. **Konsument/Entscheidung** — Makima, das auf Basis des gemeldeten States (Menü
   offen/zu, welches Menü, welche Items) die Bindings umschaltet.

**Die eigentliche offene Frage:** Lohnt sich eine Rust-Portierung des Backends, oder
sollte Makima stattdessen per IPC mit dem bestehenden **Python**-Backend sprechen?
Wichtiger Kontext, der das beeinflusst: Unser komplettes Backend basiert direkt auf
vendortem **Orca**-Code (`vendor/orca_ax/` — `AXObject`, `AXComponent`,
`AXCacheManager`, per `vendor/upgrade-orca.sh` synchron mit Upstream gehalten) — Orca
selbst ist in Python geschrieben. Eine Rust-Neuimplementierung würde diesen direkten
Code-Reuse-Vorteil verlieren und müsste die ganze AT-SPI-Zugriffsschicht neu bauen.
Nicht klar, ob das an Geschwindigkeit überhaupt etwas bringen würde (die dominanten
Kosten diese Session waren fast durchgehend D-Bus-Roundtrip-Latenz, nicht
Python-Interpreter-Overhead — siehe die komplette Caching-Arbeit weiter oben im
Ticket/in dieser Session).

Alternative, vom Nutzer selbst vorgeschlagen: Backend bleibt Python, Makima spricht per
IPC mit ihm, bekommt darüber eigenen State gemeldet (z.B. "Eingabefeld fokussiert",
"Menü X ist offen/zu") und schaltet Bindings anhand der Makima-Config um, die auf
diesen State reagiert — kein Rewrite nötig, nur eine klare State/IPC-Schnittstelle.

**Entschieden (Session vom 2026-10-02):** IPC statt Rust-Rewrite. Backend bleibt ein
eigenständiges Python-Programm, meldet sich per IPC bei Makima. Begründung: Der
Flaschenhals ist D-Bus/AT-SPI-Latenz (727µs/Call, gemessen), nicht Python-Overhead —
ändert sich mit Rust nicht. Der Wert des Backends steckt im angesammelten,
toolkit-spezifischen Wissen (Firefox/plasmashell/Qt/KIO/DBusMenu-Macken), das bei
einem Rewrite 1:1 neu nachgebaut werden müsste, gegen unreifere Rust-AT-SPI-Bindings.
Makimas eigene Latenz-kritische Domäne (Stick→Cursor, rohe Eingabeverarbeitung) ist
ohnehin eine andere Baustelle, wo sich Rust lohnt — nicht die AT-SPI-Seite.

---

## 18. IPC-Protokoll-Skizze: Backend → Makima → Decory HUD (Session vom 2026-10-02)

Verfeinerung von §17s Drei-Schichten-Idee, mit konkreterer Aufgabenteilung:

**Backend → Makima** (schlanke, zweckgebaute Event-Sprache — Makima soll möglichst
wenig über AT-SPI selbst wissen müssen):
- Element fokussiert: welcher Typ/Rolle (z.B. Textfeld, Button, Menüeintrag)
- Element nicht mehr fokussiert
- Menü geöffnet / Menü geschlossen (mit Identität, siehe §8)
- Untermenü geöffnet / Untermenü geschlossen (eigene Identität, unabhängig vom
  äußeren Menü)

**Makimas Rolle** (zentraler Entscheider, nicht nur Durchleiter):
- Reagiert auf die Backend-Events, schaltet Bindings um (siehe §15/§16).
- Entscheidet, **welches Binding zu welchem Element gehört** (Zuordnungslogik aus
  §5/§16 lebt hier, nicht im Backend).
- Schreibt den **State fürs HUD** — ein Array von Einträgen, jeweils:
  - ein Rechteck (vom Backend gemeldete Element-Position/-Größe)
  - eine Seite, an die etwas angeheftet werden soll (links/rechts/etc. — siehe
    §9s Links-Badge + Rand-Fall-Fix aus dem Prototyp)
  - das Binding/Label, das dort angezeigt werden soll
- Makima besitzt also die Entscheidung "was wird eingekreist und mit welchem Label"
  — nicht das Backend (liefert nur Rohdaten: Rects + Rollen) und nicht das HUD
  (zeichnet nur, was im State steht).

**Decory HUD**: reiner Renderer, konsumiert nur das von Makima geschriebene
Rect+Seite+Label-Array, kein eigenes AT-SPI-Wissen.

**Noch offen:** konkreter IPC-Mechanismus (D-Bus wie bisher, oder etwas anderes) —
hier bewusst nicht vorentschieden, nur die Nachrichten-/Verantwortungs-Aufteilung
festgehalten.

## 19. Vorausschau: Element-Typ-spezifische Aktionen (spekulativ, Session vom 2026-10-02)

Idee des Nutzers, explizit als unausgereift markiert: über die reinen
Menü-Shortcuts hinaus könnte man beim Fokussieren bestimmter Element-**Typen**
(z.B. ein Textblock) automatisch zusätzliche, dort sonst nicht verfügbare Aktionen
anbieten. Nutzer weiß selbst noch nicht, was das konkret sein könnte ("ich weiß
jetzt noch nicht was das sein könnte ehrlich gesagt") — nur die generelle Richtung:
"aus AT-SPI noch einiges an Modernisierung rausholen", über reine Navigation hinaus.
**Nicht weiter ausgearbeitet, nur als Richtung vermerkt** für spätere Sessions.

---

## 20. Offset-Korrektur-Saga: Regression-Postmortem (Session 2026-10-09 bis 2026-10-12)

**Status beim Pausieren: REGRESSION.** Wir hatten zwischenzeitlich einen Stand, der
vom Nutzer explizit als gut bestätigt wurde, und sind seitdem durch mehrere
Nachbesserungen in einen schlechteren Zustand geraten als den ursprünglichen. Diese
Sektion hält fest, was zuletzt sicher funktionierte, was wir seitdem kaputtgemacht
haben, und warum — bevor hier irgendjemand (menschlich oder KI) weiterpatcht, ohne
diese Geschichte zu kennen.

### 20.1 Der Stand, der nachweislich gut funktionierte

Nach der PID-Keying-Korrektur für kded6 (Submenüs erben die Korrektur ihres
Eltern-PIDs, s.o.) bestätigte der Nutzer explizit: *"Ja, die beiden Ebenen stimmen
jetzt."* Das war VOR dem active-pid-Umbau.

Danach, nach dem Umbau auf **aktives-Fenster-Offset statt eigenem-Menü-PID-Offset**
(ausgelöst durch den vom Nutzer zurecht als unautorisiert kritisierten
"kleinstes-Fenster"-Versuch — s.u.), bestätigte das Log einen stabilen Zustand: beim
Öffnen/Submenü-Öffnen/Schließen/Wiederöffnen blieb der Offset über mehrere
Render-Zyklen hinweg exakt `(7, 63.72727272727273)`, kein einziger Sprung. Das war
der Code-Zustand:

```python
def _resolve_correction_offset(self, menu):
    pid = Atspi.Accessible.get_process_id(menu)
    if pid in self._broken_pids:
        return self._active_offset(), pid
    rect = Atspi.Component.get_extents(menu, Atspi.CoordType.SCREEN)
    if rect.x > 0 or rect.y > 0:
        return (0, 0), pid          # nicht exakt (0,0) -> gilt als schon korrekt
    offset = self._active_offset()  # Offset des AKTIVEN Fensters, nicht des Menü-PIDs
    if offset != (0, 0):
        self._broken_pids.add(pid)
    return offset, pid

def _active_offset(self):
    active_pid = self._window_tracker.active_pid
    return self._window_tracker.client_offset_by_pid.get(active_pid, (0, 0))
```

Kurz: **exakt `(0,0)` → Offset des aktiven Fensters verwenden; alles andere → gilt
als schon korrekt, keine Korrektur.** Einfach, schmal, und für die damals
getesteten Fälle (kded6 Global Menu, mehrstufige Submenüs, Fenster-Verschiebung)
nachweislich stabil.

### 20.2 Was seitdem passiert ist — Chronologie

1. **Fremdeinwirkung (nicht unser Fehler, aber Auslöser):** Der Nutzer schickte eine
   Nachricht versehentlich in eine andere, parallele Claude-Session statt hierher.
   Diese Session machte eigene Änderungen an denselben Dateien — additiv (ein
   `ATSPIEventSink` für Makima-IPC, harmlos) — UND lud offenbar das persistente
   KWin-Tracker-Skript über dieselbe Ad-hoc-`/Scripting`-Route neu, die wir selbst
   schon einmal benutzt hatten und die das Skript von "dauerhaft aktiv" in
   "läuft einmal, dann inaktiv" kippt (derselbe Fehler, den wir vorher schon einmal
   mit `qdbus-qt6 org.kde.KWin /KWin reconfigure` repariert hatten). Dieses
   Zusammentreffen hat vermutlich die anschließende Verwirrung ("komplett falsch,
   wieder alles bei 0,0") mitverursacht — nicht nur unser Code.

2. **Neuer, echter Fund:** Das System-Settings-/Dolphin-Hamburger-Menü (Kirigami-
   Popup) meldet eine Position, die NICHT exakt `(0,0)` ist (z.B. `(230,41)`),
   aber trotzdem falsch — window-relativ statt Bildschirm-absolut, exakt dieselbe
   Fensterrelativitäts-Bug-Klasse, die `deckery_filter.py` für normale Elemente
   schon lange kennt und korrigiert (`resolve_offset()`). Unser exakt-`(0,0)`-Gate
   aus 20.1 erkennt das nicht, lässt diese Menüs unkorrigiert.

3. **Reaktion darauf — zu groß gedacht:** Statt das Gate gezielt zu erweitern, habe
   ich `_resolve_correction_offset` komplett durch `deckery_filter.resolve_offset()`
   ersetzt — angewendet auf den EIGENEN PID jedes Menüs, bedingungslos, mit kded6
   als einzigem (namensbasiertem) Sonderfall. Das hat den Kirigami-Fall gelöst,
   aber **das Systemtray-Flyout kaputtgemacht** (gehostet von `kwin_wayland`
   selbst, PID ohne eigenes von `resolve_offset()` matchbares Fenster — fällt
   immer auf `(0,0)` zurück, keine Korrektur, Badges "weit unten rechts").

4. **Re-Kombination der Signale:** exakt `(0,0)` → aktives Fenster (deckt kded6 UND
   das kwin_wayland-Tray ab); sonst `resolve_offset()` versuchen, nur übernehmen
   wenn kein Fallback. Für den konkret getesteten Kirigami-Fall korrekt — aber:

5. **Nächster Fund:** `resolve_offset()`s exakter `(pid, caption)`-Treffer ist
   unzuverlässig, wenn ein PID mehrere Fenster mit IDENTISCHER Caption hostet —
   nicht nur kded6 (`caption='kded6'`), sondern auch `plasmashell`
   (`caption='plasmashell'` für Tray-Flyouts, Fenstermenü-Popup, OSDs etc.
   gleichermaßen). Badges landeten hunderte Pixel daneben.

6. **Generalisierter Fix (nutzerseitig gewünscht statt Namens-Sonderfall):**
   `deckery_tracker.py` zählt jetzt, wie viele Fenster sich einen `(pid,caption)`-
   Schlüssel pro Payload teilen (`client_offset_by_key_count`); `resolve_offset()`
   vertraut dem exakten Match nur noch bei Eindeutigkeit (`count == 1`).

7. **Neuester, noch ungelöster Fund:** Für genau dasselbe `plasmashell`-Menü wechselt
   der berechnete Offset MITTEN im selben offenen Menü von `(1098,254)` auf
   `(486,24)` — und der zweite Wert ist exakt identisch mit der rohen, unkorrigierten
   Position selbst. Addition verdoppelt sie dann (`486+486=972` statt `486`).
   Strukturelle Ursache vermutet: `resolve_offset()`s Modell geht grundsätzlich davon
   aus, der gefundene Wert sei ein **Delta**, das zur fensterrelativen Rohposition
   addiert werden muss — das gilt für normale Elemente immer, aber nicht
   zwangsläufig für ein Menü, dessen Rohposition bereits bildschirmabsolut ist UND
   bei dem `resolve_offset()` zufällig auf dasselbe (inzwischen unabhängig von KWin
   getrackte) Popup-Fenster trifft. Dann ist der gefundene Wert keine Korrektur,
   sondern schlicht die (schon korrekte) Position selbst — Addition ist falsch,
   es müsste eine Ersetzung sein. **Nicht weiter verfolgt, nur hier festgehalten.**

### 20.3 Zu hinterfragende Annahmen

- **"Eine einzige, für alle Menütypen gültige Korrekturfunktion ist möglich."**
  Fraglich. Die vier gefundenen Fehlerklassen (kded6, kwin_wayland-Tray,
  Kirigami-Popups, plasmashell-Doppelkorrektur) könnten strukturell verschiedene
  Dinge sein, die zufällig alle als "Position ist falsch" auftreten, aber
  unterschiedliche Korrekturen brauchen (Ersetzung vs. Addition, Delta vs.
  Absolutwert) statt einer gemeinsamen.
- **"`resolve_offset()` aus der Produktions-Pipeline ist 1:1 auf Menüs übertragbar."**
  War die Prämisse von Schritt 3 oben. `resolve_offset()` wurde für Elemente
  INNERHALB eines bekannten Fensters entworfen (Delta-Semantik passt dort immer).
  Menüs sind selbst manchmal eigenständige Top-Level-Surfaces mit eigener,
  unabhängig von ihrem "Elternfenster" korrekter Position — dafür passt das Modell
  nicht ohne Weiteres.
- **"Mehr Allgemeingültigkeit ist immer besser als ein benannter Sonderfall."**
  Der Nutzer hat das in 20.2 Schritt 6 bewusst so gewählt (statt kded6+plasmashell
  hart zu kodieren) — nachvollziehbar als Prinzip, hat aber in Schritt 7 sofort den
  nächsten, subtileren Fehler freigelegt statt ihn zu lösen. Eventuell ist für ein
  *Prototyp*-Stadium der schmale, benannte Sonderfall (20.1) tatsächlich die
  pragmatischere Wahl, auch wenn er nicht jeden Fall abdeckt.

### 20.4 Empfehlung für den Wiedereinstieg

1. Erwägen, `menu_badge_prototype.py`s Offset-Logik auf den **20.1-Stand
   zurückzusetzen** (exakt-`(0,0)`-Gate + aktives-Fenster-Offset, sonst keine
   Korrektur) als stabile Basis — er deckt nachweislich kded6, Submenüs und
   Fenster-Verschiebung ab. Die `resolve_offset()`-Generalisierung (Schritte 3–7)
   dann als separater, noch nicht spruchreifer Versuch behandeln statt als
   Fortschritt gegenüber 20.1.
2. Den Kirigami-Popup-Fall (System Settings/Dolphin-Hamburger-Menü) als bekannte,
   dokumentierte Lücke stehen lassen, statt ihn um jeden Preis mitzulösen — er war
   nie Teil des ursprünglich bestätigten Stands.
3. Bevor weiter an `resolve_offset()` für Menüs gefeilt wird: klären, ob dessen
   Delta-Semantik für Top-Level-Menü-Surfaces überhaupt das richtige Modell ist
   (siehe 20.3) — sonst wird hier vermutlich eine fünfte Fehlerklasse gefunden.
4. Das automatische Screenshot-nach-jedem-Menü-Event-Tooling (`tools/menu_screenshots/`,
   per `distrobox-host-exec spectacle` mit Retry wegen eines bestätigten
   Verbindungsaufbau-Race beim ersten Aufruf) hat sich bewährt und sollte für die
   nächste Testrunde weiterverwendet werden.

### 20.5 Nachtrag: 20.1-Stand gegengetestet (Session 2026-10-15)

Nach dem Zurücksetzen auf 20.1 wurden die zuvor unklaren Fälle gezielt mit
Screenshots + angereicherten Logs (erster Menüeintrag, Caption des aktiven
Fensters — zur programmatischen Unterscheidung mehrdeutiger Menünamen) erneut
getestet. Ergebnis: **20.1 hält.** Alle visuell geprüften Fälle waren korrekt
oder durch bereits bekannte, separate Ursachen erklärt — keine neue
Fehlerklasse:

- **Deckery-Tray-Menü + Submenüs** (`plasmashell`, z.B. "Deckery (dev)",
  "Controller Bindings"): korrekt, `offset=(0,0)` (rohe Position bereits
  absolut).
- **Konsole Global Menu + Submenü** (`kded6`): korrekt, `offset=(7,63.7)` via
  aktives-Fenster-Substitution — wie in 20.1 erwartet.
- **Fenstermenü "Desktops"/"All Desktops"** (`kwin_wayland`-gehostet,
  `pid=12959`): Badges werden an plausiblen, korrekten Koordinaten gezeichnet
  (`_draw`-Log bestätigt), sind aber **unsichtbar** — das Fenstermenü-Popup
  rendert über unserem Layer-Shell-Overlay. Das ist das **alte, bereits
  dokumentierte Z-Order-Problem** (xdg_popup-Compositor-Konvention, siehe
  frühere KWin-`keepBelow`/`layer`-Recherche in diesem Dokument), keine neue
  Positionierungslücke. Noch kein zuverlässiger Workaround gefunden — die
  Idee, probehalber "immer ans andere Ende" zu rendern, wurde vom Nutzer
  selbst als nicht ausgereift eingestuft.
- **System-Settings-Hamburger-Menü** (Kirigami-Popup): weiterhin falsch, wie
  erwartet — bekannte, bewusst unkorrigierte Lücke (siehe 20.4 Punkt 2).

**Neuer, separater Fund — Firefox-Menü nach Vollbild→Fenstermodus-Wechsel:**
Badges erschienen an der Position, die zur alten (linken) Fensterposition
gepasst hätte, obwohl das Fenster inzwischen rechts auf dem Bildschirm steht
(Nutzer hatte Firefox zuvor im Vollbildmodus, dann in den Fenstermodus
gewechselt). Wichtig: `offset=(0,0)` — wir haben hier **gar keine** Korrektur
angewendet, die rohe AT-SPI-Position wurde unverändert übernommen. Das
bedeutet: **nicht unsere Korrekturlogik ist falsch**, sondern Firefox selbst
meldet eine veraltete Position über AT-SPI. Auffällig auch: Das Menü meldet
eine ANDERE PID (`15111`) als das aktive Firefox-Hauptfenster (`15151`) —
Firefox' Multi-Prozess-Architektur (Content-Prozess vs. Browser-Prozess).
Vermutung: der Content-Prozess, der das Menü rendert, hat seine interne
Vorstellung der Fenstergrenzen nach dem Vollbild-Exit nicht aktualisiert.
**Außerhalb unseres Einflussbereichs** — kein Korrekturpfad in 20.1 könnte
das adressieren, da wir für diesen Fall bewusst gar nicht korrigieren.
Nicht weiter verfolgt, nur hier festgehalten, falls es später wieder auffällt.
