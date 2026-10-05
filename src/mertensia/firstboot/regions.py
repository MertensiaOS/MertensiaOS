"""System regional choices, searchable metadata and local time previews.

This module has no GTK dependency so the system metadata can be used and tested
without loading the first-boot application.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import gettext
import json
from pathlib import Path
import re
import subprocess
from typing import NamedTuple
import unicodedata
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
