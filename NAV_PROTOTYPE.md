# Navigations-Prototyp (`deckery_nav/`)

**Status:** Erster Prototyp, 2026-11-12. Nichts an den Modi ist entschieden —
alles sind umschaltbare Presets in `deckery_nav/presets.toml` zum Vergleichen.
Bedien-Anwendung im Sinne von [BACKEND_V2_DESIGN.md](BACKEND_V2_DESIGN.md) §10.

## Starten

```
tools/run_nav.sh                      # echter Desktop (Backend muss laufen)
python3 -m deckery_nav.ctl list       # Presets
python3 -m deckery_nav.ctl next relative     # Stick-Variante weiterschalten
python3 -m deckery_nav.ctl next absolute     # Pad-Variante weiterschalten
python3 -m deckery_nav.ctl set absolute zaeh
python3 -m deckery_nav.ctl state
```

`presets.toml` wird beim Speichern live neu gelesen.

## Was er tut

Zwei Eingaben gleichzeitig, unabhängig voneinander:

- **Linker Stick → relativ („Sprung“):** Auslenken springt zum nächsten Ziel
  in Richtung, Halten wiederholt. Varianten: `kante` (Zeilen/Spalten zuerst,
  wie Browser-Spatial-Navigation), `kegel` (nächstes Zentrum im Kegel),
  `strahl` (erstes Ziel, das ein Strahl trifft); 4 / 8 Richtungen / freier
  Winkel.
- **Linkes Pad → absolut („Tastkarte“):** Pad = Bildschirm (oder Monitor,
  Fenster der Auswahl, Box um alle Ziele); der Finger wählt das Ziel darunter
  bzw. in der Nähe, mit Hysterese, optional Verweilzeit und Glättung. Zeiger
  springt aufs Ziel oder folgt dem Finger.

Beide teilen sich die Auswahl: mit dem Pad grob hin, mit dem Stick weiter.
Overlay: Auswahl (cyan), Stick-Richtung + Vorschau des Sprungziels (gelb),
Finger + Fangradius (pink), unten links aktive Presets und Warnungen.

## Woher die Eingaben kommen (ohne Makima-Änderung)

**Vorübergehende Notlösung, nur zum Testen** (Makima-Besitzer + Nutzer,
2026-11-12): langfristig bekommt spatnav einen eigenen Kanal (B-13, Vorschlag
unten). Nichts Dauerhaftes auf `makima_input.py` aufbauen.

Makima schreibt mit `analog-state-export on` Stick- und Pad-Werte in
`$XDG_RUNTIME_DIR/makima-state.json`. Linker Stick und linkes Pad sind in der
Deckery-Config frei (`disabled`). `--ensure-analog-export` (in `run_nav.sh`
gesetzt) schaltet den Export ein, wenn er aus ist (höchstens 1×/s), und beim
Beenden wieder aus, außer makima ist pausiert (= HUD offen) — so mit dem
Makima-Besitzer abgesprochen. Zeiger: KWin-EIS (`org.kde.KWin.EIS.RemoteDesktop`),
absolute logische Koordinaten, keine Rückfrage auf dem echten Desktop.

Grenzen dieses Wegs: ~19 KB JSON pro Änderung, Rate durch makimas
Schreib-Schwelle (0,0045) begrenzt, Polling mit 125 Hz → spürbare, aber
kleine Latenz. Keine Haptik.

## Vorschlag Makima-Brücke (nicht umgesetzt)

1. **Haptik-Kommando** über `makima-control.sock`, z.B.
   `haptic left|right <duration_us> <count> <gain_db>` — Pulse beim
   Auswahlwechsel (Tastkarte: „Kante ertastet“, Sprung: „eingerastet“).
2. **Analog-Kanal** mit Pad-/Stick-Rate statt state.json (siehe B-13):
   nur linkes Pad (x, y, touching, pressed) und linker Stick, binär oder
   Zeilen-JSON über einen eigenen Socket, Ziel < 5 ms.

## Wünsche ans Backend (nicht umgesetzt, gemeldet)

- Fenster-Liste mit Geometrie + aktivem Fenster (für Bereich „Fenster“ statt
  der Box um dessen Ziele, und um beim Start im aktiven Fenster zu beginnen).
- Zustände pro Element (`focused`, `checked`, `selected`, `expanded`) —
  Startpunkt am Tastaturfokus, Anzeige.
- Zeigerposition kennt keiner der Clients (Wayland); KWin hat sie
  (`workspace.cursorPos`) — wenn sich der Nutzer mit dem rechten Pad bewegt,
  weiß der Prototyp nicht, wo der Zeiger jetzt ist.

## Tests

- `tests/test_nav.py` — Richtungswahl, Stick-Zustandsautomat, Pad-Abbildung,
  Hysterese/Verweilen, Presets, makima-Parsing, Navigationszustand.
- `DECKERY_TESTDESK=4 tools/testdesk/run_box.sh python3 tools/testdesk/nav_check.py`
  — fährt den Prototyp im Testdesktop mit einer simulierten makima-Datei
  (Stick-Sprünge, gehaltener Stick, Pad-Wischen in drei Presets, Screenshots).
