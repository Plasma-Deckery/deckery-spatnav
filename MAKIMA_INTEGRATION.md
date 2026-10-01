# Makima-Integration — Architektur-Ticket

**Status:** Architektur entschieden, Details offen. Konsolidiert die
Architektur-relevanten Teile aus [MENU_HIJACK_DESIGN.md](MENU_HIJACK_DESIGN.md)
(§14-19, Session vom 2026-10-02). Dort bleiben die Toolkit-spezifischen Befunde
(Firefox/plasmashell/Dolphin/Trayscale-Macken, Prototyp-Bugs) — hier geht es um die
größere Frage: wie wird das, was in `deckery-spatnav` prototypet wird, Teil von
**Decory Makima** (dem Produktiv-Tool für Controller-Input-Bindings)?

---

## 1. Ziel

`deckery-spatnav` ist der Prototyping-Nebenschauplatz: hier wird in Python gegen
echte AT-SPI-Eigenheiten getestet (siehe [REQUIREMENTS.md](REQUIREMENTS.md),
[MENU_HIJACK_DESIGN.md](MENU_HIJACK_DESIGN.md)). Features werden hier erst
funktionsfähig gemacht, dann **Stück für Stück nach Makima übertragen** — nicht als
Rewrite, sondern als eigenständiges Backend, mit dem Makima per IPC spricht.

Erstes konkretes Feature auf diesem Weg: Menü-Hijacking (Controller-Shortcuts neben
Menüeinträgen) — siehe MENU_HIJACK_DESIGN.md für den Stand dort.

---

## 2. Drei-Schichten-Architektur (entschieden)

```
┌─────────────────┐      IPC       ┌──────────┐      State      ┌─────────────┐
│  AT-SPI-Backend  │ ───events───▶  │  Makima  │ ───(Rect,Seite, │ Decory HUD  │
│  (Python,        │                │          │     Label)[]──▶ │ (Renderer)  │
│  eigenständiges  │                │          │                 │             │
│  Programm)       │                │          │                 │             │
└─────────────────┘                └──────────┘                 └─────────────┘
```

1. **AT-SPI-Backend** (das, was in `deckery-spatnav` entsteht): eigenständiges
   Python-Programm. Liefert nur Rohdaten — "dieses Element existiert, diese Rolle,
   diese Position" — und Lifecycle-Events (fokussiert/nicht mehr fokussiert,
   Menü/Untermenü auf/zu). Kein Wissen über Bindings, Controller, oder Zeichnen.
2. **Makima**: zentraler Entscheider. Reagiert auf Backend-Events, schaltet
   Controller-Bindings um, entscheidet **welches Binding zu welchem Element gehört**,
   schreibt den Render-State fürs HUD.
3. **Decory HUD** (`~/.local/share/deckery/deckery-hud/`): reiner Renderer. Konsumiert
   nur ein Array von `{Rechteck, Anheft-Seite, Label}`-Einträgen, kein eigenes
   AT-SPI-Wissen.

**Wichtig:** Die Entscheidung "was wird markiert und mit welchem Label" liegt bei
**Makima**, nicht beim Backend und nicht beim HUD. Das Backend liefert nur Fakten,
das HUD zeichnet nur, was ihm gesagt wird.

---

## 3. Sprache/Technologie: Python-Backend + IPC, kein Rust-Rewrite (entschieden)

Begründung (siehe MENU_HIJACK_DESIGN.md §17 für die volle Herleitung):

- **Flaschenhals ist D-Bus/AT-SPI-Latenz** (gemessen: ~727µs pro `get_rect()`-Call),
  nicht Python-Interpreter-Overhead. Eine Rust-Portierung würde den eigentlichen
  Engpass nicht berühren.
- **Der Wert des Backends steckt im angesammelten, toolkit-spezifischen Wissen**
  (Firefox-, plasmashell-, Qt/QML-, KIO-, DBusMenu-Eigenheiten — siehe
  MENU_HIJACK_DESIGN.md), nicht im Code selbst. Ein Rewrite müsste das alles 1:1
  neu nachbauen, gegen unreifere Rust-AT-SPI-Bindings.
- Das Backend basiert direkt auf vendortem **Orca**-Code (`vendor/orca_ax/`,
  synchron mit Upstream via `vendor/upgrade-orca.sh`) — Orca selbst ist Python.
- **Makimas eigene Latenz-kritische Domäne** (Stick→Cursor, rohe
  Eingabeverarbeitung) ist eine andere Baustelle, wo sich Rust tatsächlich lohnt —
  nicht die AT-SPI-Seite, die strukturell IPC-gebunden bleibt, egal in welcher
  Sprache.

---

## 4. IPC-Protokoll — Skizze (Details offen)

**Backend → Makima**, schlanke Event-Sprache (Makima soll möglichst wenig über
AT-SPI selbst wissen müssen):
- Element fokussiert (mit Typ/Rolle)
- Element nicht mehr fokussiert
- Menü geöffnet / geschlossen (mit stabiler Identität)
- Untermenü geöffnet / geschlossen (eigene Identität, unabhängig vom äußeren Menü —
  siehe MENU_HIJACK_DESIGN.md §8 zur Notwendigkeit eindeutiger Submenü-Identität)

**Makima → Decory HUD**: Array von `{Rechteck, Anheft-Seite, Label}` — siehe §2.

**Noch offen:**
- Konkreter IPC-Transport (D-Bus wie der Rest des Projekts, oder etwas anderes?)
- Exaktes Nachrichtenformat/Schema
- Wie granular die "Element fokussiert"-Events sein sollen (nur Menüs, oder auch
  normale Fokus-Wechsel außerhalb von Menüs? Siehe §6)

---

## 5. Bindings-Override-Schema (aus MENU_HIJACK_DESIGN.md §15, hier referenziert)

Solange ein Menü offen ist: alle normalen Bindings deaktiviert außer Escape, Enter,
Pfeiltasten/D-Pad (bleiben als Fallback-Navigation aktiv). Übrige Tasten werden den
Menüeinträgen zugeordnet (D-Pad×Face-Buttons + L1/L2/R1/R2 als Modifier — siehe
MENU_HIJACK_DESIGN.md §5). Untermenü-Ebene hat eigene, vom äußeren Menü unabhängige
Bindings. Strategie (welches Kombi-Schema) soll konfigurierbar sein, analog zum
bestehenden `cursor_nav_tray.py`-Strategie-Umschalter.

Offene Spannung (nicht entschieden, siehe MENU_HIJACK_DESIGN.md §16): direktes
Binding pro Ziel vs. mehrstufiges Paging (L1/R1 durch Gruppen) — Tendenz zu direkten
Bindings, aber nicht final.

---

## 6. Vorausschau: über Menüs hinaus (spekulativ)

Noch unausgereifte Idee (MENU_HIJACK_DESIGN.md §19): element-typ-spezifische
Zusatzaktionen beim Fokussieren bestimmter AT-SPI-Rollen (z.B. Textblöcke) — welche
Aktionen das sein könnten, ist noch offen. Nur als Richtung vermerkt, nicht Teil des
aktuellen Scopes.

---

## 7. Offene Fragen für die Umsetzung

1. IPC-Transport-Entscheidung (§4).
2. Nachrichtenschema/Versionierung zwischen Backend und Makima.
3. Wie wird das Backend als eigenständiger Prozess lebenszyklus-verwaltet (von
   Makima gestartet? Eigener systemd-Service? Tray-Toggle wie `cursor_nav_tray.py`?).
4. Migrationsreihenfolge: welche Features aus `deckery-spatnav` wandern zuerst nach
   Makima (Menü-Hijacking zuerst, dann Cursor-Nav aus
   [CURSOR_NAV_DESIGN.md](CURSOR_NAV_DESIGN.md)? Oder umgekehrt?).
5. Wie interagieren Menü-Hijacking-Modus und Cursor-Nav-Modus innerhalb von Makima —
   exklusiv oder gleichzeitig aktiv (offene Frage auch in
   MENU_HIJACK_DESIGN.md §11.7)?
