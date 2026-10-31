#!/usr/bin/python3
"""Render the curated light/dark SVG wallpapers at 3840 × 2160."""

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg


ROOT = Path(__file__).resolve().parents[1]


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    viewport = Rsvg.Rectangle()
    for source in sorted((ROOT / "branding/wallpapers").glob("*.svg")):
        document = ET.parse(source).getroot()
        # Give off-canvas ellipses room to blur before cropping the final frame.
        # Rendering directly into the frame clips Violet Hour's glow at its edges.
        padding = 1024 if document.find(".//{http://www.w3.org/2000/svg}filter") is not None else 0
        if padding:
            document.set("viewBox", "-512 -512 2944 2104")
            document.set("width", str(3840 + padding * 2))
            document.set("height", str(2160 + padding * 2))
        viewport.x, viewport.y = 0, 0
        viewport.width, viewport.height = 3840 + padding * 2, 2160 + padding * 2
        artwork = Rsvg.Handle.new_from_data(ET.tostring(document))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(viewport.width), int(viewport.height))
        artwork.render_document(cairo.Context(surface), viewport)
        result = cairo.ImageSurface(cairo.FORMAT_RGB24, 3840, 2160)
        context = cairo.Context(result)
        context.set_source_surface(surface, -padding, -padding)
        context.paint()
        result.write_to_png(str(output / source.with_suffix(".png").name))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "system/backgrounds/mertensia")
    generate(parser.parse_args().output)
