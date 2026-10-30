#!/usr/bin/env python3
"""Reuse an ISO only when its immutable inputs and complete contents match."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(arguments):
    action, filename, *inputs = arguments
    image = Path(filename)
    state = image.with_suffix(".iso-cache.json")
    key = hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
    if action == "check":
        try:
            saved = json.loads(state.read_text())
            if saved.get("inputs") != key or not image.is_file():
                return 1
            return 0 if saved.get("sha256") == checksum(image) else 1
        except (OSError, ValueError, AttributeError):
            return 1
    if action == "invalidate":
        state.unlink(missing_ok=True)
        return 0
    if action != "record":
        raise SystemExit(f"Unknown ISO cache action: {action}")
    # A failed build must never publish a reusable cache entry.
    if not image.is_file() or image.stat().st_size == 0:
        raise SystemExit(f"Builder did not produce a nonempty ISO: {image}")
    saved = {"inputs": key, "sha256": checksum(image)}
    with tempfile.NamedTemporaryFile(mode="w", dir=state.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(saved, stream)
        stream.write("\n")
    try:
        temporary.chmod(0o644)
        temporary.replace(state)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
