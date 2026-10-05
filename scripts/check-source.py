#!/usr/bin/env python3
"""Validate source syntax and container inputs without writing bytecode."""
import ast
import json
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORIES = ("src", "branding", "installer", "system", "scripts", "tests")


def check_file(path):
    with path.open("rb") as stream:
        shebang = stream.readline(256).strip()
    if path.suffix == ".json":
        json.loads(path.read_text())
    elif path.suffix == ".py" or shebang.startswith(b"#!/usr/bin/python3"):
        ast.parse(path.read_text(), filename=str(path))
    elif shebang == b"#!/bin/sh":
        subprocess.run(["sh", "-n", str(path)], check=True)
    elif path.suffix == ".sh" or shebang in (
        b"#!/usr/bin/env bash", b"#!/usr/bin/bash", b"#!/bin/bash",
    ):
        subprocess.run(["bash", "-n", str(path)], check=True)


def check_source():
    for directory in SOURCE_DIRECTORIES:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                check_file(path)

    for path in [*ROOT.glob("Containerfile*"), *(ROOT / "tests/integration").glob("Containerfile*")]:
        for line in path.read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            fields = shlex.split(line)
            if any(field.startswith("--from=") for field in fields):
                continue  # Another build stage supplies these inputs.
            sources = [field for field in fields[1:-1] if not field.startswith("--")]
            for source in sources:
                if not (ROOT / source).exists():
                    raise SystemExit(f"{path.name}: missing build input {source}")
    print("Python, shell and JSON syntax and container build inputs are valid.")


if __name__ == "__main__":
    check_source()
