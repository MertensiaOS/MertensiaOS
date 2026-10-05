"""JSON event protocol and checked subprocess execution for installer operations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class InstallError(RuntimeError):
    pass


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, separators=(",", ":")), flush=True)


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        input=input_text,
        text=True,
        check=False,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        detail = result.stderr.strip()
        command = Path(argv[0]).name
        raise InstallError(
            f"{command} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )

    return result
