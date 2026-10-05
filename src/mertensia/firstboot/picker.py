"""Searchable GTK picker for the system's supported regional choices."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from .regions import REGIONAL_HELP, search_text


class RegionalPicker(Adw.Dialog):
    """A searchable, keyboard-accessible picker that keeps the stored ID separate."""

    def __init__(self, key, choices, selected, on_selected):
        title, description, placeholder = REGIONAL_HELP[key]
        super().__init__(title=title, content_width=560, content_height=600)
        self.add_css_class("regional-picker")
        self.choices = sorted(choices, key=lambda choice: choice.value != selected)
        self.selected = selected
        self.on_selected = on_selected
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=20, margin_end=20, margin_bottom=20)
        box.append(Gtk.Label(label=description, wrap=True, xalign=0, css_classes=["dim-label"]))
        self.search = Gtk.SearchEntry(placeholder_text=placeholder, hexpand=True, css_classes=["regional-search"])
        self.search.update_property([Gtk.AccessibleProperty.LABEL], [placeholder])
        box.append(self.search)
        self.count = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        box.append(self.count)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                   css_classes=["boxed-list"])
        self.rows = []
        for choice in self.choices:
            row = Adw.ActionRow(title=choice.title, subtitle=choice.subtitle, activatable=True,
                                title_lines=2, subtitle_lines=2, use_markup=False)
            row.choice = choice
            row.search_terms = search_text(" ".join(choice))
            if choice.value == selected:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic", css_classes=["accent"]))
                row.update_property([Gtk.AccessibleProperty.DESCRIPTION], ["Current selection"])
            row.connect("activated", self._select)
            self.results.append(row)
            self.rows.append(row)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroll.set_child(self.results)
        self.empty = Adw.StatusPage(title="No matches found", description="Try another name or a shorter search.",
                                    icon_name="system-search-symbolic", visible=False, vexpand=True)
        box.append(self.scroll)
        box.append(self.empty)
        toolbar.set_content(box)
        self.set_child(toolbar)
        self.set_focus(self.search)
        self.search.connect("changed", self._filter)
        self.search.connect("activate", self._activate_search)
        self.search.connect("stop-search", self._stop_search)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._search_key)
        self.search.add_controller(keys)
        self._filter()

    def _filter(self, *_args):
        terms = search_text(self.search.get_text()).split()
        self.visible_rows = []
        for row in self.rows:
            visible = all(term in row.search_terms for term in terms)
            row.set_visible(visible)
            if visible:
                self.visible_rows.append(row)
        count = len(self.visible_rows)
        self.count.set_text(f"{count} {'match' if count == 1 else 'matches'}" if terms else "Current selection first · All available options")
        self.scroll.set_visible(count > 0)
        self.empty.set_visible(count == 0)
        self.scroll.get_vadjustment().set_value(0)

    def _select(self, row):
        self.on_selected(row.choice)
        self.close()

    def _activate_search(self, *_args):
        if len(self.visible_rows) == 1:
            self._select(self.visible_rows[0])
        elif self.visible_rows:
            self._focus_first_result()

    def _focus_first_result(self):
        self.visible_rows[0].grab_focus()
        # Filtering can happen in the same frame as the key press. Do not let
        # the row's previous position in the unfiltered list scroll it away.
        self.scroll.get_vadjustment().set_value(0)

    def _search_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Down and self.visible_rows:
            self._focus_first_result()
            return True
        return False

    def _stop_search(self, *_args):
        if self.search.get_text():
            self.search.set_text("")
        else:
            self.close()
