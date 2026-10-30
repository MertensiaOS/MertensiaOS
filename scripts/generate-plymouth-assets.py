#!/usr/bin/python3
"""Render Plymouth's project-owned PNGs from the shared brand; no image inputs change."""

import argparse
import json
import math
from pathlib import Path

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg


ROOT = Path(__file__).resolve().parents[1]


def color(context, value):
    context.set_source_rgb(*(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)))


def rounded_rectangle(context, x, y, width, height, radius):
    for cx, cy, angle in ((x + width - radius, y + radius, -90),
                          (x + width - radius, y + height - radius, 0),
                          (x + radius, y + height - radius, 90),
                          (x + radius, y + radius, 180)):
        context.arc(cx, cy, radius, math.radians(angle), math.radians(angle + 90))
    context.close_path()


def generate(output):
    palette = json.loads((ROOT / "branding/brand-tokens.json").read_text())["palette"]
    output.mkdir(parents=True, exist_ok=True)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 112, 112)
    bloom = Rsvg.Handle.new_from_file(str(ROOT / "branding/assets/soft-bloom-tint.svg"))
    viewport = Rsvg.Rectangle()
    viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, 112, 112
    bloom.render_document(cairo.Context(surface), viewport)
    surface.write_to_png(str(output / "watermark.png"))

    # two-step's unmasked question text is black. A light interior keeps that
    # native path readable as well as the masked password path.
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 44)
    context = cairo.Context(surface)
    rounded_rectangle(context, 0, 0, 320, 44, 8)
    color(context, palette["darkSurface"])
    context.fill()
    rounded_rectangle(context, 3, 3, 314, 38, 5)
    color(context, palette["canvas"])
    context.fill()
    surface.write_to_png(str(output / "entry.png"))

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    context = cairo.Context(surface)
    color(context, palette["ink"])
    context.arc(5, 5, 3, 0, 2 * math.pi)
    context.fill()
    surface.write_to_png(str(output / "bullet.png"))

    # Reuse the symbolic key master without changing GTK's source artwork.
    source = (ROOT / "branding/icons/mertensia-key-symbolic.svg").read_text()
    key = Rsvg.Handle.new_from_data(source.replace("#2e3436", palette["tint"]).encode())
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 35, 44)
    viewport.x, viewport.y, viewport.width, viewport.height = 5, 10, 24, 24
    key.render_document(cairo.Context(surface), viewport)
    surface.write_to_png(str(output / "lock.png"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "branding/plymouth")
    generate(parser.parse_args().output)
