"""GTK4 test application with known content, for the v2 backend's scenarios
(tools/testdesk/scenarios.py, gtk_*) and the mode probe (mode_probe.py).

Every control has a fixed, unique accessible name so scenarios can find it
in the backend's target list. Disabled and hidden controls are there to
check they are *not* targets; the scroll containers are long enough to
scroll; menu button, popover, drop-down and dialog open their own
surfaces (xdg_popup / modal toplevel).

Run in the deckery distrobox (GTK4 bindings live there):
    tools/testdesk/run_box.sh python3 tools/testdesk/gtk_testapp.py   # test desktop
    distrobox enter deckery -- python3 tools/testdesk/gtk_testapp.py  # real desktop

Prints one line per user action ("clicked Aktiver Knopf", …) and mirrors
it in the status line at the bottom ("Zuletzt: …")."""

import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

APP_ID = "org.deckery.TestApp"

LIST_ROWS = 40
TREE_FOLDERS = 15
TREE_CHILDREN = 5
SCROLL_AREA_ROWS = 24


def named(widget, name):
    """Sets the accessible name (AT-SPI Name) explicitly, for widgets
    without a label of their own (scale, spin button, entry, …)."""
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    return widget


class TestWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Deckery Testapp")
        self.set_default_size(1100, 690)
        self._status = Gtk.Label(label="Zuletzt: –", xalign=0)
        named(self._status, "Statuszeile")

        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        columns.append(self._controls())
        columns.append(self._scroll_area())
        lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        lists.append(self._list())
        lists.append(self._tree())
        columns.append(lists)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(12)
        outer.append(columns)
        outer.append(self._status)
        self.set_child(outer)

    def report(self, text):
        print(text, flush=True)
        self._status.set_label(f"Zuletzt: {text}")

    # ---- left column: one row per kind of control ----

    def _controls(self):
        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        row = 0

        def add(caption, *widgets):
            nonlocal row
            grid.attach(Gtk.Label(label=caption, xalign=0), 0, row, 1, 1)
            box = Gtk.Box(spacing=6)
            for w in widgets:
                box.append(w)
            grid.attach(box, 1, row, 1, 1)
            row += 1

        add("Knöpfe", self._button("Aktiver Knopf"), self._button("Deaktivierter Knopf", sensitive=False),
            self._button("Versteckter Knopf", visible=False))

        toggle = Gtk.ToggleButton(label="Umschaltknopf")
        toggle.connect("toggled", lambda b: self.report(f"toggled Umschaltknopf {b.get_active()}"))
        switch = named(Gtk.Switch(valign=Gtk.Align.CENTER), "Schalter")
        switch.connect("notify::active", lambda s, _: self.report(f"switched Schalter {s.get_active()}"))
        switch_off = named(Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False), "Schalter deaktiviert")
        add("Umschalter", toggle, switch, switch_off)

        check = Gtk.CheckButton(label="Häkchen")
        check.connect("toggled", lambda b: self.report(f"checked Häkchen {b.get_active()}"))
        add("Häkchen", check, Gtk.CheckButton(label="Häkchen deaktiviert", sensitive=False))

        radios = []
        for label in ("Radio Eins", "Radio Zwei", "Radio Drei"):
            radio = Gtk.CheckButton(label=label, group=radios[0] if radios else None)
            radio.connect("toggled", lambda b, l=label: b.get_active() and self.report(f"selected {l}"))
            radios.append(radio)
        radios[0].set_active(True)
        add("Radio-Gruppe", *radios)

        scale = named(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1), "Schieberegler")
        scale.set_size_request(220, -1)
        scale.set_value(30)
        scale.connect("value-changed", lambda s: self.report(f"value Schieberegler {s.get_value():.0f}"))
        add("Schieberegler", scale)

        spin = named(Gtk.SpinButton.new_with_range(0, 100, 1), "Zahlenfeld")
        spin.set_value(5)
        spin.connect("value-changed", lambda s: self.report(f"value Zahlenfeld {s.get_value():.0f}"))
        add("Zahlenfeld", spin)

        drop = named(Gtk.DropDown.new_from_strings(["Apfel", "Birne", "Kirsche", "Pflaume"]), "Auswahlliste")
        drop.connect("notify::selected", lambda d, _: self.report(
            f"selected Auswahlliste {d.get_selected_item().get_string()}"))
        add("Auswahlliste", drop)

        entry = named(Gtk.Entry(placeholder_text="hier tippen"), "Textfeld")
        entry.connect("activate", lambda e: self.report(f"entered Textfeld {e.get_text()!r}"))
        add("Textfeld", entry, named(Gtk.Entry(text="gesperrt", sensitive=False), "Textfeld deaktiviert"))

        add("Passwortfeld", named(Gtk.PasswordEntry(show_peek_icon=True), "Passwortfeld"))

        text = named(Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR), "Mehrzeiliger Text")
        text.get_buffer().set_text("Erste Zeile\nZweite Zeile\nDritte Zeile\nVierte Zeile\nFünfte Zeile")
        scrolled = Gtk.ScrolledWindow(child=text, min_content_height=64, min_content_width=220)
        scrolled.set_has_frame(True)
        add("Mehrzeilig", scrolled)

        add("Menüs", self._menu_button(), self._popover_button())

        dialog = Gtk.Button(label="Dialog öffnen")
        dialog.connect("clicked", lambda b: self._open_dialog())
        link = Gtk.LinkButton(uri="https://example.org", label="Ein Link")
        # Never start a browser from a test: just report the click.
        link.connect("activate-link", lambda b: self.report("clicked Ein Link") or True)
        add("Dialog, Link", dialog, link)

        group = Gtk.Frame(label="Deaktivierte Gruppe", sensitive=False)
        group.set_child(self._button("Knopf in deaktivierter Gruppe"))
        add("Gruppe", group)
        return grid

    def _button(self, label, **props):
        button = Gtk.Button(label=label, **props)
        button.connect("clicked", lambda b: self.report(f"clicked {label}"))
        return button

    def _menu_button(self):
        menu = Gio.Menu()
        menu.append("Menüeintrag Eins", "win.menu-eins")
        menu.append("Menüeintrag Zwei", "win.menu-zwei")
        sub = Gio.Menu()
        sub.append("Untereintrag", "win.menu-unter")
        menu.append_submenu("Untermenü", sub)
        section = Gio.Menu()
        section.append("Ankreuzeintrag", "win.menu-kreuz")
        menu.append_section(None, section)
        for name in ("menu-eins", "menu-zwei", "menu-unter"):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, _, n=name: self.report(f"activated {n}"))
            self.add_action(action)
        kreuz = Gio.SimpleAction.new_stateful("menu-kreuz", None, GLib.Variant("b", False))
        kreuz.connect("activate", lambda a, _: (a.set_state(GLib.Variant("b", not a.get_state().unpack())),
                                                 self.report(f"toggled menu-kreuz {a.get_state().unpack()}")))
        self.add_action(kreuz)
        return Gtk.MenuButton(label="Menü öffnen", menu_model=menu)

    def _popover_button(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._button("Knopf im Popover"))
        box.append(Gtk.CheckButton(label="Häkchen im Popover"))
        box.append(named(Gtk.Entry(placeholder_text="Popover-Feld"), "Feld im Popover"))
        return Gtk.MenuButton(label="Popover öffnen", popover=Gtk.Popover(child=box))

    def _open_dialog(self):
        self.report("clicked Dialog öffnen")
        dialog = Gtk.Window(title="Testdialog", transient_for=self, modal=True)
        dialog.set_default_size(380, 180)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        box.append(Gtk.Label(label="Ein modaler Dialog.", xalign=0))
        box.append(named(Gtk.Entry(placeholder_text="im Dialog"), "Dialogfeld"))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        for label in ("Abbrechen", "OK"):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda b, l=label: (self.report(f"clicked {l}"), dialog.close()))
            buttons.append(b)
        box.append(buttons)
        dialog.set_child(box)
        dialog.present()

    # ---- middle: mixed controls in a scroll container ----

    def _scroll_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for i in range(1, SCROLL_AREA_ROWS + 1):
            kind = i % 4
            if kind == 1:
                box.append(self._button(f"Scrollknopf {i}"))
            elif kind == 2:
                box.append(Gtk.CheckButton(label=f"Scrollhäkchen {i}"))
            elif kind == 3:
                box.append(named(Gtk.Entry(placeholder_text=f"Feld {i}"), f"Scrollfeld {i}"))
            else:
                box.append(named(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 10, 1),
                                 f"Scrollregler {i}"))
        box.append(self._button("Scrollknopf ganz unten"))
        scrolled = Gtk.ScrolledWindow(child=box, min_content_width=240, vexpand=True)
        scrolled.set_has_frame(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        named(scrolled, "Scrollbereich")
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame.append(Gtk.Label(label="Scrollbereich", xalign=0))
        frame.append(scrolled)
        return frame

    # ---- right: list (widget rows) and tree (recycled ListView rows) ----

    def _list(self):
        listbox = named(Gtk.ListBox(), "Liste")
        for i in range(1, LIST_ROWS + 1):
            row = Gtk.ListBoxRow(child=Gtk.Label(label=f"Listenzeile {i}", xalign=0))
            named(row, f"Listenzeile {i}")
            listbox.append(row)
        listbox.connect("row-activated", lambda l, r: self.report(f"activated Listenzeile {r.get_index() + 1}"))
        scrolled = Gtk.ScrolledWindow(child=listbox, min_content_height=220, min_content_width=260)
        scrolled.set_has_frame(True)
        named(scrolled, "Listenbereich")
        return scrolled

    def _tree(self):
        root = Gio.ListStore(item_type=Gtk.StringObject)
        for i in range(1, TREE_FOLDERS + 1):
            root.append(Gtk.StringObject.new(f"Ordner {i}"))

        def children(item):
            text = item.get_string()
            if not text.startswith("Ordner "):
                return None
            n = text.split()[1]
            store = Gio.ListStore(item_type=Gtk.StringObject)
            for j in range(1, TREE_CHILDREN + 1):
                store.append(Gtk.StringObject.new(f"Eintrag {n}.{j}"))
            return store

        model = Gtk.TreeListModel.new(root, False, False, children)
        factory = Gtk.SignalListItemFactory()

        def setup(_, item):
            expander = Gtk.TreeExpander()
            expander.set_child(Gtk.Label(xalign=0))
            item.set_child(expander)

        def bind(_, item):
            row = item.get_item()
            text = row.get_item().get_string()
            expander = item.get_child()
            expander.set_list_row(row)
            expander.get_child().set_label(text)
            item.set_accessible_label(text)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        view = named(Gtk.ListView(model=Gtk.SingleSelection(model=model), factory=factory), "Baum")
        view.connect("activate", lambda v, pos: self.report(
            f"activated {model.get_item(pos).get_item().get_string()}"))
        scrolled = Gtk.ScrolledWindow(child=view, min_content_height=220, min_content_width=260, vexpand=True)
        scrolled.set_has_frame(True)
        named(scrolled, "Baumbereich")
        return scrolled


class TestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        TestWindow(self).present()


if __name__ == "__main__":
    sys.exit(TestApp().run(sys.argv))
