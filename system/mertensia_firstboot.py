#!/usr/bin/python3
"""First-boot and additional encrypted-account UI."""

from __future__ import annotations

import argparse
from datetime import datetime
from functools import lru_cache
import gettext
import json
import re
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import NamedTuple
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402


MARKER = Path("/var/lib/mertensia/firstboot-complete")
HELPER = "/usr/libexec/mertensia-accounts-helper"
MAX_PASSWORD_BYTES = 256
BRANDING = Path(__file__).resolve().parents[1] / "installer/branding"
if not BRANDING.is_dir():
    BRANDING = Path("/usr/share/mertensia/branding")


REGIONAL_FIELDS = (
    ("locale", "Language", "en_US.UTF-8", ["localectl", "list-locales"]),
    ("keymap", "Keyboard layout", "us", ["localectl", "list-keymaps"]),
    ("timezone", "Time zone", "UTC", ["timedatectl", "list-timezones"]),
)

REGIONAL_HELP = {
    "locale": ("Choose your language", "The language and regional formats used after setup.",
               "Search languages or countries"),
    "keymap": ("Choose your keyboard layout", "Match the layout printed on your keyboard. It takes effect after setup.",
               "Search layouts, e.g. English or Dvorak"),
    "timezone": ("Choose your time zone", "Choose a nearby city in your time zone. Daylight saving is handled automatically.",
                 "Search cities, countries or time zones"),
}


class RegionalChoice(NamedTuple):
    value: str
    title: str
    subtitle: str


def search_text(text: str) -> str:
    """Match names without requiring accents, punctuation or exact casing."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c))


@lru_cache(maxsize=None)
def iso_names(standard: str) -> dict[str, str]:
    try:
        records = json.loads(Path(f"/usr/share/iso-codes/json/iso_{standard}.json").read_text())[standard]
    except (OSError, ValueError, KeyError):
        return {}
    return {record[key]: record.get("common_name", record["name"])
            for record in records for key in ("alpha_2", "alpha_3") if key in record}


@lru_cache(maxsize=None)
def native_language(language: str, name: str) -> str:
    return gettext.translation("iso_639-3", languages=[language], fallback=True).gettext(name)


@lru_cache(maxsize=1)
def keyboard_names() -> dict[str, str]:
    """Use the same console-to-XKB mapping as localectl, when available."""
    layouts = {}
    try:
        root = ET.parse("/usr/share/X11/xkb/rules/evdev.xml")
        for layout in root.findall(".//layoutList/layout"):
            name = layout.findtext("configItem/name")
            layouts[name] = layout.findtext("configItem/description") or name
            for variant in layout.findall("variantList/variant"):
                key = f"{name}-{variant.findtext('configItem/name')}"
                layouts[key] = variant.findtext("configItem/description") or key
    except (OSError, ET.ParseError):
        pass
    names = dict(layouts)
    try:
        lines = Path("/usr/share/systemd/kbd-model-map").read_text().splitlines()
    except OSError:
        lines = []
    mapped = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or line.startswith("#") or fields[0] in mapped:
            continue
        console, layout, _model, variant = fields[:4]
        layout, variant = layout.split(",")[0], variant.split(",")[0]
        key = layout if variant in ("", "-") else f"{layout}-{variant}"
        if key in layouts:
            names[console] = layouts[key]
            mapped.add(console)
    return names


@lru_cache(maxsize=1)
def timezone_countries() -> dict[str, str]:
    countries = iso_names("3166-1")
    zones = {}
    # zone.tab includes country-specific aliases as well as canonical zones.
    for filename in ("zone1970.tab", "zone.tab"):
        try:
            lines = (Path("/usr/share/zoneinfo") / filename).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split("\t")
            if line.startswith("#") or len(fields) < 3:
                continue
            zones[fields[2]] = ", ".join(countries.get(code, code) for code in fields[0].split(","))
    return zones


def timezone_preview(value: str, now: datetime | None = None) -> str:
    try:
        zone = ZoneInfo(value)
        local = now.astimezone(zone) if now else datetime.now(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return value
    offset = local.utcoffset()
    minutes = int(offset.total_seconds() / 60) if offset else 0
    sign = "+" if minutes >= 0 else "−"
    hours, minutes = divmod(abs(minutes), 60)
    return f"{local:%a, %d %b · %H:%M} · UTC{sign}{hours:02d}:{minutes:02d}"


def regional_choices(key: str, values: list[str]) -> list[RegionalChoice]:
    choices = []
    for value in values:
        if key == "locale":
            language, country = value.split(".")[0].split("_", 1)
            name = iso_names("639-3").get(language, language)
            region = iso_names("3166-1").get(country, country)
            native = native_language(language, name)
            title = f"{name} ({region})"
            subtitle = f"{native} · {value}" if native != name else value
        elif key == "keymap":
            title = keyboard_names().get(value, value.replace("_", " ").replace("-", " · "))
            subtitle = value
        else:
            parts = value.replace("_", " ").split("/")
            country = timezone_countries().get(value, "")
            title = "Coordinated Universal Time" if value in ("UTC", "Etc/UTC") else parts[-1]
            if country:
                title += f" ({country})"
            subtitle = value.replace("_", " ").replace("/", " / ")
        choices.append(RegionalChoice(value, title, subtitle))
    return sorted(choices, key=lambda choice: (search_text(choice.title), choice.value))


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


def regional_options() -> dict[str, list[str]]:
    """Offer only values supported by the installed system."""
    options = {}
    for key, title, _default, command in REGIONAL_FIELDS:
        result = subprocess.run(command, text=True, capture_output=True, check=True, timeout=10)
        values = sorted(set(result.stdout.splitlines()))
        if key == "locale":
            # C/POSIX are not regional locales accepted by the account helper.
            values = [value for value in values if re.fullmatch(r"[A-Za-z]{2,3}_[A-Za-z]{2}(?:\.[A-Za-z0-9@_-]+)?", value)]
        if not values:
            raise ValueError(f"No available options for {title.lower()}.")
        options[key] = values
    return options


def preferred_option(values: list[str], preferred: str) -> int:
    # locale tools may spell the same encoding as UTF-8 or utf8.
    normalize = lambda value: value.lower().replace("-", "")
    return next((index for index, value in enumerate(values)
                 if normalize(value) == normalize(preferred)), 0)


def account_error(values: dict) -> tuple[str, str] | None:
    if not values["real_name"].strip() or len(values["real_name"].strip()) > 128:
        return "real_name", "Enter your full name (up to 128 characters)."
    username = values["username"].strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", username) or username == "mertensia-setup":
        return "username", "Use up to 31 lowercase letters, numbers, underscores or hyphens. Start with a letter or underscore."
    password = values["password"]
    try:
        password_bytes = len(password.encode("utf-8"))
    except UnicodeEncodeError:
        return "password", "The password contains an unsupported character."
    if len(password) < 8 or any(c in password for c in "\x00\n\r"):
        return "password", "Choose a password with at least 8 characters and no line breaks."
    if password_bytes > MAX_PASSWORD_BYTES:
        return "password", "Shorten your password to at most 256 bytes. Some characters use more than one byte."
    if password != values["confirm"]:
        return "confirm", "The passwords do not match. Enter the same password again."
    return None


class SetupWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, firstboot: bool):
        super().__init__(application=app, title="MertensiaOS Setup" if firstboot else "Mertensia Accounts")
        self.firstboot = firstboot
        self.busy = False
        self.entries = {}
        self.selectors = {}
        self.region_choices = {}
        self.selected_regions = {}
        self.set_default_size(900, 760)
        self.add_css_class("installer")
        self.add_css_class("firstboot")
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(BRANDING / "icons"))
        self.style_manager = Adw.StyleManager.get_default()
        self.css_provider = None
        self._update_style()
        self.style_manager.connect("notify::dark", self._update_style)
        self.style_manager.connect("notify::high-contrast", self._update_style)
        self.connect("close-request", lambda *_: self.busy)
        if firstboot:
            self.fullscreen()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="MertensiaOS", css_classes=["brand-name"]))
        header.add_css_class("flat")
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=180, vhomogeneous=False, hhomogeneous=False)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)
        if firstboot:
            box = self._page("device", "Make this computer yours", "Choose a device name and the regional settings for your system.",
                             "Step 1 of 2 · Your computer", "mertensia-computer-symbolic")
            group = Adw.PreferencesGroup(title="Device", description="The name used to identify this computer on your network.")
            self._entry(group, "hostname", "Device name")
            box.append(group)
            group = Adw.PreferencesGroup(title="Language and region", description="Make yourself at home. You can change these later in Settings.")
            icons = {"locale": "preferences-desktop-locale-symbolic", "keymap": "input-keyboard-symbolic",
                     "timezone": "alarm-symbolic"}
            for key, title, _default, _command in REGIONAL_FIELDS:
                row = Adw.ActionRow(title=title, subtitle="Loading available options…", sensitive=False,
                                    activatable=True, subtitle_lines=2, use_markup=False)
                row.add_prefix(Gtk.Image(icon_name=icons[key], pixel_size=20, css_classes=["accent"]))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _row, key=key: self._choose_region(key))
                self.selectors[key] = row
                group.add(row)
            box.append(group)
            self.time_preview = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["secondary-text"])
            box.append(self.time_preview)
            self.device_message = self._message(box)
            self.retry_regions = Gtk.Button(label="Retry loading settings", visible=False, halign=Gtk.Align.START)
            self.retry_regions.connect("clicked", self._load_regions)
            box.append(self.retry_regions)
            self.continue_button = self._actions(box, ("Continue", self._next, "suggested-action"))[0]
            self.continue_button.set_sensitive(False)
            self._load_regions()
            self.clock_source = GLib.timeout_add_seconds(30, self._update_time_preview)
            self.connect("unrealize", lambda *_: GLib.source_remove(self.clock_source))
        box = self._page("account", "Create your account", "Your password protects your personal files and unlocks your encrypted home.",
                         "Step 2 of 2 · Your account" if firstboot else "A private space of their own", "mertensia-key-symbolic")
        group = Adw.PreferencesGroup(title="About you")
        self._entry(group, "real_name", "Full name")
        self._entry(group, "username", "Username")
        box.append(group)
        group = Adw.PreferencesGroup(title="Protect your files", description="Use at least 8 characters. There is no separate recovery key for your home; keep your password somewhere safe.")
        self._entry(group, "password", "Password", password=True)
        self._entry(group, "confirm", "Confirm password", password=True)
        box.append(group)
        if not firstboot:
            self.admin = Gtk.CheckButton(label="Make this user an administrator")
            box.append(self.admin)
        else:
            box.append(Gtk.Label(label="Your first account will be an administrator.", xalign=0, wrap=True, css_classes=["secondary-text"]))
        self.message = self._message(box)
        actions = [("Back", lambda *_: self.stack.set_visible_child_name("device"), "flat")] if firstboot else []
        actions.append(("Create account", self._submit, "suggested-action"))
        self.submit = self._actions(box, *actions)[-1]
        box = self._page("working", "Creating your account", "Setting up your encrypted home. This may take a little while.",
                         "Finishing setup", "mertensia-install-symbolic")
        self.spinner = Gtk.Spinner(spinning=False, halign=Gtk.Align.START, width_request=32, height_request=32)
        box.append(self.spinner)
        box = self._page("complete", "You’re all set", "Your encrypted account is ready. Use your new password to sign in.",
                         "Welcome to MertensiaOS", "mertensia-check-symbolic")
        self.completion_message = Gtk.Label(label="Opening the sign-in screen…" if firstboot else "The account is ready to use.",
                                            wrap=True, xalign=0, css_classes=["secondary-text"])
        box.append(self.completion_message)
        if not firstboot:
            self._actions(box, ("Done", lambda *_: self.close(), "suggested-action"))
        self.stack.set_visible_child_name("device" if firstboot else "account")

    def _update_style(self, *_args):
        if self.css_provider:
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.css_provider)
        theme = json.loads((BRANDING / "brand-tokens.json").read_text())["themes"]["dark" if self.style_manager.get_dark() else "light"]
        roles = {"window_bg_color": "background", "window_fg_color": "text", "view_bg_color": "surface",
                 "view_fg_color": "text", "card_bg_color": "surface", "card_fg_color": "text",
                 "headerbar_bg_color": "background", "headerbar_fg_color": "text", "accent_bg_color": "action",
                 "accent_fg_color": "onAction", "accent_color": "link"}
        colors = "" if self.style_manager.get_high_contrast() else "\n".join(f"@define-color {name} {theme[role]};" for name, role in roles.items())
        colors += "\n@define-color brand_muted @window_fg_color; @define-color brand_border @window_fg_color;"
        self.css_provider = Gtk.CssProvider()
        self.css_provider.load_from_data((colors + "\n" + (BRANDING / "installer.css").read_text() + "\n" + (BRANDING / "firstboot.css").read_text()).encode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _page(self, name, title, body, step, icon):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=688, tightening_threshold=520, unit=Adw.LengthUnit.PX)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24, valign=Gtk.Align.CENTER)
        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(32)
        image.set_halign(Gtk.Align.START)
        image.add_css_class("page-icon")
        if name == "complete":
            image.add_css_class("success-icon")
        intro.append(image)
        intro.append(Gtk.Label(label=step, xalign=0, css_classes=["step-label"]))
        intro.append(Gtk.Label(label=title, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                css_classes=["page-title"], accessible_role=Gtk.AccessibleRole.HEADING))
        intro.append(Gtk.Label(label=body, xalign=0, wrap=True, css_classes=["page-description"]))
        box.append(intro)
        clamp.set_child(box)
        scroll.set_child(clamp)
        self.stack.add_named(scroll, name)
        return box

    def _entry(self, group, key, title, value="", password=False):
        entry = Adw.PasswordEntryRow(title=title) if password else Adw.EntryRow(title=title)
        entry.set_text(value)
        self.entries[key] = entry
        group.add(entry)

    def _load_regions(self, *_args):
        self.retry_regions.set_visible(False)
        self.device_message.set_visible(False)
        self.continue_button.set_sensitive(False)
        for row in self.selectors.values():
            row.set_sensitive(False)
            row.set_subtitle("Loading available options…")

        def worker():
            try:
                options = regional_options()
            except (OSError, subprocess.SubprocessError, ValueError):
                GLib.idle_add(self._regions_loaded, None)
            else:
                GLib.idle_add(self._regions_loaded, options)

        threading.Thread(target=worker, daemon=True).start()

    def _regions_loaded(self, options):
        if options is None:
            self.continue_button.set_sensitive(False)
            for row in self.selectors.values():
                row.set_sensitive(False)
                row.set_subtitle("Settings unavailable · Retry below")
            self.device_message.set_text("Could not load languages, keyboard layouts and time zones. Try loading them again.")
            self.device_message.set_visible(True)
            self.retry_regions.set_visible(True)
            return False
        self.retry_regions.set_visible(False)
        self.device_message.set_visible(False)
        for key, _title, default, _command in REGIONAL_FIELDS:
            choices = regional_choices(key, options[key])
            self.region_choices[key] = choices
            previous = self.selected_regions.get(key)
            preferred = previous.value if previous else default
            choice = choices[preferred_option([choice.value for choice in choices], preferred)]
            self._region_selected(key, choice)
            self.selectors[key].set_sensitive(True)
        self.continue_button.set_sensitive(True)
        return False

    def _choose_region(self, key):
        picker = RegionalPicker(key, self.region_choices[key], self.selected_regions[key].value,
                                lambda choice: self._region_selected(key, choice))
        picker.present(self)

    def _region_selected(self, key, choice):
        self.selected_regions[key] = choice
        self.selectors[key].set_subtitle(choice.title)
        self._update_time_preview()

    def _update_time_preview(self):
        choice = self.selected_regions.get("timezone")
        if choice:
            self.time_preview.set_text(f"Local time: {timezone_preview(choice.value)}")
            self.time_preview.set_visible(True)
        return True

    def _regional_values(self):
        return {key: self.selected_regions[key].value if key in self.selected_regions else ""
                for key in self.selectors}

    def _message(self, box):
        label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, visible=False, css_classes=["error"])
        box.append(label)
        return label

    def _actions(self, box, *actions):
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                          column_spacing=12, row_spacing=12, max_children_per_line=len(actions), css_classes=["actions"])
        buttons = []
        for title, callback, style in actions:
            button = Gtk.Button(label=title, css_classes=["action-button", style])
            button.connect("clicked", callback)
            row.insert(button, -1)
            buttons.append(button)
        box.append(row)
        return buttons

    def _next(self, *_args):
        hostname = self.entries["hostname"].get_text().strip()
        if not re.fullmatch(r"(?=.{1,253}$)(?!-)(?:[a-zA-Z0-9-]{1,63})(?<!-)(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*", hostname):
            self.device_message.set_text("Enter a device name using letters, numbers or hyphens. Start and end each part with a letter or number.")
            self.device_message.set_visible(True)
            self.entries["hostname"].grab_focus()
            return
        if not self.continue_button.get_sensitive() or not all(self._regional_values().values()):
            self.device_message.set_text("Wait for the regional settings to load, then choose an option in each selector.")
            self.device_message.set_visible(True)
            return
        self.device_message.set_visible(False)
        self.stack.set_visible_child_name("account")

    def _submit(self, *_args) -> None:
        if self.busy:
            return
        values = {key: entry.get_text() for key, entry in self.entries.items()}
        for entry in self.entries.values():
            entry.remove_css_class("error")
        error = account_error(values)
        if error:
            key, message = error
            self.message.set_text(message)
            self.message.set_visible(True)
            self.entries[key].add_css_class("error")
            self.entries[key].grab_focus()
            return
        values.pop("confirm")
        if self.firstboot:
            values.update(self._regional_values())
        values["firstboot"] = self.firstboot
        values["admin"] = True if self.firstboot else self.admin.get_active()
        self.submit.set_sensitive(False)
        self.busy = True
        self.message.set_visible(False)
        self.spinner.start()
        self.stack.set_visible_child_name("working")

        def worker():
            try:
                data = self._create_account(values)
                if not isinstance(data, dict):
                    raise ValueError("The account service returned an invalid response.")
            except Exception as error:
                data = {"event": "error", "message": str(error)}
            values["password"] = ""
            GLib.idle_add(self._done, data)
        threading.Thread(target=worker, daemon=True).start()

    def _create_account(self, values):
        result = subprocess.run(["pkexec", HELPER], input=json.dumps(values) + "\n", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return json.loads(result.stdout.splitlines()[-1])

    def _done(self, data: dict) -> bool:
        self.busy = False
        self.spinner.stop()
        if data.get("event") == "complete":
            self.entries["password"].set_text("")
            self.entries["confirm"].set_text("")
            warning = data.get("warning")
            if isinstance(warning, str) and warning:
                self.completion_message.set_text(warning)
            self.stack.set_visible_child_name("complete")
        else:
            self.message.set_text(data.get("message", "Account creation failed."))
            self.message.set_visible(True)
            self.submit.set_sensitive(True)
            self.stack.set_visible_child_name("account")
        return False


class SetupApp(Adw.Application):
    def __init__(self, firstboot: bool):
        super().__init__(application_id="org.mertensia.Firstboot" if firstboot else "org.mertensia.Accounts")
        self.firstboot = firstboot

    def do_activate(self):
        if self.firstboot and MARKER.exists():
            self.quit()
            return
        window = self.get_active_window() or SetupWindow(self, self.firstboot)
        if self.firstboot:
            window.fullscreen()
        window.present()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--add-user", action="store_true")
    args = parser.parse_args()
    return SetupApp(not args.add_user).run()


if __name__ == "__main__":
    raise SystemExit(main())
