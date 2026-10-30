#!/usr/bin/env python3
"""Install declared image files from the mounted build input stage."""

import json
from pathlib import Path
import shutil
import sys


def install(manifest, source_root, image_root):
    for source, destination in json.loads(manifest.read_text()):
        source = source_root / source
        destination = image_root / destination.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


if __name__ == "__main__":
    install(*(Path(argument) for argument in sys.argv[1:]))
