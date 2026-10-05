#!/usr/bin/python3
"""Render first-boot and add-account screens without creating accounts."""

import argparse
import importlib.util
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("firstboot_ui", Path(__file__).resolve().parents[1] / "system/mertensia_firstboot.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
Adw, GLib, Gtk = ui.Adw, ui.GLib, ui.Gtk
from gi.repository import Gio


class PreviewWindow(ui.SetupWindow):
    options = {
        "locale": ["de_DE.UTF-8", "en_GB.UTF-8", "en_NZ.UTF-8", "en_US.UTF-8", "es_ES.UTF-8",
                   "fr_CA.UTF-8", "fr_FR.UTF-8", "ja_JP.UTF-8", "pt_BR.UTF-8", "zh_CN.UTF-8"],
        "timezone": ["America/New_York", "America/St_Johns", "Asia/Kathmandu", "Asia/Kolkata",
                     "Europe/Berlin", "Europe/London", "Pacific/Auckland", "Pacific/Chatham", "UTC"],
        "keymap": ["de", "dvorak", "fr", "fr-bepo", "uk", "us", "us-intl"],
    }

    def _load_regions(self, *_args):
        self._regions_loaded(self.options)

    def _create_account(self, values):
        return {"event": "complete"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=760)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--add-user", action="store_true")
    parser.add_argument("--system-options", action="store_true", help="Read the installed system's real regional lists")
    parser.add_argument("--check-selectors", action="store_true", help="Exercise picker search, selection and retry states")
    args = parser.parse_args()
    if args.system_options:
        PreviewWindow.options = ui.regional_options()
        print("System options: " + ", ".join(f"{len(values)} {key}" for key, values in PreviewWindow.options.items()), flush=True)
    app = Adw.Application(application_id="org.mertensia.Firstboot.Preview", flags=Gio.ApplicationFlags.NON_UNIQUE)
    errors = []

    def activate(app):
        try:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK if args.theme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
            window = PreviewWindow(app, not args.add_user)
            window.unfullscreen()
            window.set_default_size(args.width, args.height)
            theme = Gtk.IconTheme.get_for_display(window.get_display())
            for name in ("computer", "key", "install", "check"):
                assert theme.has_icon(f"mertensia-{name}-symbolic")
            if not args.add_user:
                assert window.entries["hostname"].get_text() == ""
                window._next()
                assert window.stack.get_visible_child_name() == "device"
                assert window.device_message.get_visible()
                window.entries["hostname"].set_text("mertensia-laptop")
                window._next()
                assert window.stack.get_visible_child_name() == "account"
                window.stack.set_visible_child_name("device")
                window.entries["hostname"].set_text("")
            window.entries["real_name"].set_text("Alex Morgan")
            window.entries["username"].set_text("alex")
            window.present()
            if args.check_selectors and not args.add_user:
                check_selectors(window)
            if not args.capture:
                return
            args.capture.mkdir(parents=True, exist_ok=True)
            pages = iter((["device", "picker-locale", "picker-keymap", "picker-keymap-search", "picker-timezone", "picker-empty"]
                          if not args.add_user else []) + ["account", "error", "working", "complete"])
            window.stack.set_transition_duration(0)
            active_picker = None

            def capture(page, bottom=False):
                try:
                    if page == "picker-keymap-search":
                        assert active_picker.get_focus() == active_picker.visible_rows[0]
                        assert active_picker.scroll.get_vadjustment().get_value() == 0
                    paintable = Gtk.WidgetPaintable.new(window)
                    snapshot = Gtk.Snapshot.new()
                    paintable.snapshot(snapshot, window.get_width(), window.get_height())
                    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
                    mode = "accounts" if args.add_user else "firstboot"
                    suffix = "-bottom" if bottom else ""
                    path = args.capture / f"{mode}-{args.theme}-{args.width}-{page}{suffix}.png"
                    assert texture.save_to_png(str(path))
                    print(path, flush=True)
                    adjustment = window.stack.get_visible_child().get_vadjustment()
                    if not page.startswith("picker-") and not bottom and adjustment.get_upper() > adjustment.get_page_size():
                        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                        GLib.timeout_add(250, capture, page, True)
                    else:
                        advance()
                except Exception as error:
                    errors.append(error)
                    app.quit()
                return False

            def advance():
                nonlocal active_picker
                if active_picker:
                    active_picker.force_close()
                    active_picker = None
                page = next(pages, None)
                if page is None:
                    app.quit()
                    return False
                if page.startswith("picker-"):
                    window.stack.set_visible_child_name("device")
                    key = page.removeprefix("picker-")
                    if key == "empty":
                        key = "timezone"
                    elif key == "keymap-search":
                        key = "keymap"
                    active_picker = ui.RegionalPicker(key, window.region_choices[key], window.selected_regions[key].value,
                                                      lambda choice: None)
                    active_picker.present(window)
                    if page == "picker-empty":
                        active_picker.search.set_text("no-such-city")
                    elif page == "picker-keymap-search":
                        active_picker.search.set_text("english dvorak")
                        active_picker._search_key(None, ui.Gdk.KEY_Down, 0, 0)
                    elif key == "timezone":
                        active_picker.search.set_text("new zealand")
                elif page == "error":
                    window._done({"event": "error", "message": "The account could not be created. Check your settings and try again."})
                else:
                    window.message.set_visible(False)
                    window.stack.set_visible_child_name(page)
                window.spinner.set_spinning(page == "working")
                window.stack.get_visible_child().get_vadjustment().set_value(0)
                GLib.timeout_add(250, capture, page)
                return False

            GLib.timeout_add(300, advance)
        except Exception as error:
            errors.append(error)
            app.quit()

    app.connect("activate", activate)
    with mock.patch.object(ui.subprocess, "run", side_effect=RuntimeError("Preview blocks processes")):
        app.run([])
    if errors:
        raise errors[0]


def check_selectors(window):
    """Exercise actual GTK widgets on Broadway without applying system settings."""
    original = window._regional_values()
    for key, query, expected in (("locale", "francais france", "fr_FR"),
                                  ("keymap", "dvorak", "dvorak"),
                                  ("timezone", "new zealand auckland", "Pacific/Auckland")):
        picker = ui.RegionalPicker(key, window.region_choices[key], original[key],
                                   lambda choice, key=key: window._region_selected(key, choice))
        picker.present(window)
        picker.search.set_text("no-such-setting-12345")
        assert not picker.visible_rows and picker.empty.get_visible()
        picker.search.emit("activate")
        assert window._regional_values() == original
        picker.search.set_text(query)
        assert picker.visible_rows, query
        row = next(row for row in picker.visible_rows if row.choice.value == expected
                   or (key == "locale" and row.choice.value.startswith(expected + ".")))
        row.emit("activated")
        assert window._regional_values()[key] == row.choice.value
        original = window._regional_values()
        picker.force_close()

    # Reopening and cancelling must retain the stored identifier and put it first.
    picker = ui.RegionalPicker("timezone", window.region_choices["timezone"], original["timezone"],
                               lambda choice: window._region_selected("timezone", choice))
    picker.present(window)
    assert picker.rows[0].choice.value == original["timezone"]
    picker.search.set_text("kathmandu")
    picker.search.emit("stop-search")
    assert not picker.search.get_text()
    picker.search.emit("stop-search")
    assert window._regional_values() == original
    picker.force_close()
    window.entries["hostname"].set_text("mertensia-laptop")
    window._next()
    assert window.stack.get_visible_child_name() == "account"
    window.stack.set_visible_child_name("device")
    assert window._regional_values() == original
    window.entries["hostname"].set_text("")

    # Exercise the failure/retry UI without an actual failed system service.
    window.continue_button.set_sensitive(False)
    window._regions_loaded(None)
    assert window.retry_regions.get_visible()
    assert not window.continue_button.get_sensitive()
    window.retry_regions.emit("clicked")
    assert window.continue_button.get_sensitive()
    assert window._regional_values() == original
    assert not window.device_message.get_visible()
    assert not window.retry_regions.get_visible()
    print("PASS: picker search, no results, selection, cancel, persistence and retry", flush=True)


if __name__ == "__main__":
    main()
