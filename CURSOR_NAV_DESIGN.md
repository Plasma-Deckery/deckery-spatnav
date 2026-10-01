# Gamepad-Cursor-Navigation — Design-Brainstorm

**Status:** Brainstorm, keine Entscheidung final. Dient als Referenz für zukünftige
Sessions, wenn der Konversationskontext verloren gegangen ist. Ergänzt
[REQUIREMENTS.md](REQUIREMENTS.md) (deckt nur das reine Erkennungs-Overlay ab) um die
Navigations-/Interaktionsschicht, die noch nicht existiert.

---

## 1. Ziel

Das bestehende Overlay ([REQUIREMENTS.md](REQUIREMENTS.md)) erkennt und zeichnet
Rahmen um AT-SPI-Elemente — rein passiv, kein Input. Dieses Dokument plant den
nächsten Schritt: **tatsächlich mit dem linken Thumbstick zwischen den erkannten
Elementen navigieren** können, als Maus-Ersatz für Gamepad-Nutzung.

Kernidee des Nutzers: kein reines 4/8-Wege-Grid, sondern ein "2D-Cursor-Feeling"
mit echter Analog-Richtung — man zeigt mit dem Stick in eine beliebige Richtung,
das System snappt zum nächstgelegenen sinnvollen Ziel in genau dieser Richtung.

---

## 2. Die vier Bausteine

1. **Input-Mapping** — wie der linke Thumbstick auf "wechsle zu Element X" abgebildet wird
2. **Snap-Algorithmus** — welches Element ist "das nächste in diese Richtung"
3. **Bewegungsmodell** — wie sich der Fokusindikator zwischen zwei Elementen bewegt (Timing/Easing)
4. **Visuelle Sprache** — wie der fokussierte Zustand dargestellt wird (Rahmen, Glanz/Shader, Punkt)

---

## 3. Offene Architektur-Entscheidung: echter Cursor vs. virtueller Indikator

Das bestehende Overlay ist bewusst passiv (Click-through, keine Input-Injektion —
REQUIREMENTS.md: *"spricht nichts, steuert nichts, injiziert keine Eingaben"*).
Den echten Desktop-Cursor zu bewegen (Idee des Nutzers) ist ein **Scope-Sprung**
von "visualisieren" zu "aktiv Input injizieren" (z.B. via `ydotool` oder
Wayland-`virtual-pointer`-Protokoll).

**Nachteile des echten Cursors:**
- Springt er bei jedem Snap durch Zwischenpositionen, können andere Apps an jedem
  Zwischenpunkt ungewollte Hover-Tooltips/Mouseover-Effekte auslösen.
- Ohne separate Klick-Injektion (existiert aktuell nicht) bringt er einen nur hin,
  klicken ist ein zweiter, eigener Baustein.
- Ein "sticky" Slingshot-Timing (siehe §5) lässt sich mit dem echten OS-Cursor
  nicht sauber umsetzen, ohne dass es sich kaputt/rucklig anfühlt.

**Empfehlung (noch nicht vom Nutzer bestätigt):** Erst ein virtueller, frei
animierbarer Indikator (der "leuchtende Punkt"), echten Cursor erst am **Ende**
einer Snap-Animation dorthin setzen (wenn tatsächlich geklickt/ausgewählt wird),
nicht während der Bewegung. Sauberer zu bauen, keine Nebenwirkungen auf andere
Apps während der Navigation, und der Spring-Effekt (§5) ist am virtuellen
Indikator trivial umsetzbar.

**Offen:** Nutzer hat sich dazu noch nicht festgelegt.

---

## 4. Thumbstick-Mapping: Velocity vs. diskrete Schritte

Nutzer hat selbst zwei Optionen genannt: Velocity-basiert (feine Bewegungen) vs.
diskrete "Klicks" im 2D-Grid (große Bewegungen), evtl. sogar Hierarchie-Drilldown
bei maximaler Auslenkung (Figma-Style rein/raus).

**Einschätzung:** Für Snapping zwischen diskreten Zielen ist Velocity vermutlich
die falsche Wahl — Deadzone-/Drift-Tuning wird nervig, "Stick zeigt noch leicht
in die alte Richtung" führt zu ungewolltem Weiterspringen. Nahezu jedes
Gamepad-Dashboard/Menü (PS/Xbox-Stil) nutzt stattdessen: Auslenkung über
Schwellwert → ein Sprung in die Zeigerichtung, mit Wiederholungs-Delay
(wie Tastatur-Repeat) bei gehaltenem Stick.

**Empfehlung:** diskrete, winkelbasierte Sprünge (siehe §5) statt Velocity-Integration.

**Offen:** Hierarchie-Drilldown bei maximaler Auslenkung — siehe §6, als v2 vorgeschlagen.

---

## 5. Snap-Algorithmus (Richtung + nächstes Element)

Gelöstes Problem aus Game-UI-Navigation, nicht neu zu erfinden: Kandidaten nach
einer Kombination aus **Winkelabweichung von der Stick-Richtung** und
**Distanz** bewerten, Elemente "hinter" der aktuellen Bewegungsrichtung
ausschließen. Parametrisierung (Winkel- vs. Distanz-Gewichtung) bestimmt, ob
sich die Navigation "zackig" oder "unvorhersehbar" anfühlt — das ist der Hebel
zum Fein-Tunen, kein algorithmisches Neuland.

Das unterstützt direkt die Anforderung "echte Analog-Richtung, nicht nur
4/8-Wege" — der Stick-Winkel geht 1:1 als Score-Eingabe ein, keine Snapping auf
Kompassrichtungen nötig.

**Datenquelle:** Die Kandidatenliste (Elemente + ihre Extents) kommt direkt aus
dem bereits existierenden [ElementCache](deckery_cache.py)/[render_tree()](deckery_walk.py) —
diese Schicht muss die Navigation nicht neu bauen, nur konsumieren.

---

## 6. Bewegungsmodell: "Slingshot"-Animation

Nutzer-Idee: kein linearer Sprung, sondern eine Feder-/Slingshot-Kurve — langsam
anlaufen (leicht "sticky" am Start), schnelle Hauptbewegung, "sticky"
Abbremsen/Einrasten am Ziel. Visuell am Mittelpunkt des Zielrahmens verankert.

**Offen:** Ist der animierte Punkt der Fokus-Indikator selbst, oder ein
zusätzliches Element neben dem Rahmen um das Zielelement (oder beides
kombiniert — Rahmen um Ziel + Punkt, der dazwischen "fliegt")? Nutzer hat beide
Varianten genannt, sich nicht festgelegt.

---

## 7. Hierarchie-Drilldown (Figma-Style)

Idee: bei starker Stick-Auslenkung in Container/Gruppen "reinzoomen" und wieder
raus. **Empfehlung: v2-Feature, nicht MVP** — vermischt sonst "wohin snappen"
mit "wo bin ich gerade in der Hierarchie", MVP würde dadurch nie fertig. Erst
den flachen 2D-Snap zum Laufen bringen, dann bewerten ob es gebraucht wird.

---

## 8. Visuelle Effekte (Shader/Glanz)

Nutzer-Wunsch: nicht-intrusiver, aber sichtbarer Glanz-/Shine-Effekt um das
fokussierte Element, performant.

**Technische Implikation:** [deckery_draw.py](deckery_draw.py) zeichnet heute mit
Cairo — kein echter GPU-Shader-Pipeline-Zugriff (kein Specular/Glanz möglich).
Für sowas bräuchte es GTK4s Snapshot/GSK-Pipeline mit eigenem Fragment-Shader
(technisch machbar, aber ein echter zusätzlicher Implementierungsschritt, kein
Cairo-Oneliner).

**Empfehlung:** Snap-Mechanik zuerst mit simplem Cairo-Rahmen fühlbar machen,
Glanz-Shader als separater, späterer Schritt obendrauf — nicht gleichzeitig mit
der Navigationslogik entwickeln.

---

## 9. Vorgeschlagene Phasierung (MVP zuerst)

**Noch nicht vom Nutzer bestätigt** — als Diskussionsgrundlage:

1. **MVP:** virtueller Indikator (kein echter Cursor), diskrete
   winkelbasierte Snap-Sprünge (§4+§5), einfacher Cairo-Rahmen als
   visuelles Feedback (kein Shader), kein Hierarchie-Drilldown.
2. **v2:** Slingshot-Animation (§6) verfeinern, echten Cursor am Ziel
   platzieren + Klick-Injektion.
3. **v3:** Shader-/Glanz-Effekte (§8), Hierarchie-Drilldown (§7) falls
   nach v1/v2-Nutzung als nötig empfunden.

---

## 10. Bezug zum bestehenden Code

Diese Feature-Schicht braucht keine neue Elementerkennung — sie konsumiert, was
[deckery_cache.py](deckery_cache.py)/[deckery_walk.py](deckery_walk.py) bereits
liefert (Liste von Elementen + Extents pro Fenster, inkl. des neu eingeführten
Extents-Caches, siehe dortige Docstrings). Neu wären: Gamepad-Input-Handling,
Snap-Scoring, Animations-State-Machine, und je nach Entscheidung in §3 eine
Input-Injection-Schicht (ydotool o.ä.) für Cursor/Klick.

---

## 11. Offene Fragen (Stand: Brainstorm-Ende)

- [ ] Echter Cursor vs. virtueller Indikator (§3) — Empfehlung: virtuell zuerst
- [ ] Slingshot-Punkt = Fokus-Indikator selbst oder zusätzliches Element (§6)
- [ ] Hierarchie-Drilldown: wirklich gebraucht, oder reicht flaches 2D-Snapping (§7)?
- [ ] Phasierung aus §9 so bestätigt, oder andere Reihenfolge gewünscht?
