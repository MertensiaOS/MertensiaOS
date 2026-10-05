"""Shared branding paths, GTK theme roles and stylesheet loading."""

from __future__ import annotations

import json
from pathlib import Path


BRANDING = Path(__file__).resolve().parents[3] / "branding"
if not BRANDING.is_dir():
    BRANDING = Path("/usr/share/mertensia/branding")

THEME_ROLES = {
    "window_bg_color": "background", "window_fg_color": "text",
    "view_bg_color": "surface", "view_fg_color": "text",
    "card_bg_color": "surface", "card_fg_color": "text",
    "headerbar_bg_color": "background", "headerbar_fg_color": "text",
    "accent_bg_color": "action", "accent_fg_color": "onAction",
    "accent_color": "link",
}


def theme_colors(dark: bool, high_contrast: bool = False, *,
                 muted: str = "@window_fg_color", border: str = "@window_fg_color") -> str:
    """Build shared theme roles, preserving native high-contrast colors."""
    roles = {}
    if not high_contrast:
        theme = json.loads((BRANDING / "brand-tokens.json").read_text())["themes"]["dark" if dark else "light"]
        roles.update({name: theme[role] for name, role in THEME_ROLES.items()})
    else:
        muted = border = "@window_fg_color"
    roles.update(brand_muted=muted, brand_border=border)
    return "\n".join(f"@define-color {name} {value};" for name, value in roles.items())


def load_brand_style(display, dark: bool, high_contrast: bool = False, *,
                     stylesheets: tuple[str, ...] = ("installer.css",),
                     muted: str = "@window_fg_color", border: str = "@window_fg_color"):
    """Install the requested branded CSS and return its removable provider."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    colors = theme_colors(dark, high_contrast, muted=muted, border=border)
    css = "\n".join((BRANDING / filename).read_text() for filename in stylesheets)
    provider = Gtk.CssProvider()
    provider.load_from_data((colors + "\n" + css).encode())
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return provider
