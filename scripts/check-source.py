#!/usr/bin/env python3
"""Validate source syntax and build-context inputs without writing bytecode."""
import ast
import json
from pathlib import Path
import shlex
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
for directory in ("installer", "system", "scripts", "tests"):
    for path in (root / directory).rglob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    for path in (root / directory).rglob("*.json"):
        json.loads(path.read_text())
for path in [*(root / "scripts").glob("*.sh"), root / "system/mertensia-reseal-root", root / "system/mertensia-setup-authorized"]:
    subprocess.run(["bash", "-n", str(path)], check=True)
for path in [*root.glob("Containerfile*"), *(root / "tests/integration").glob("Containerfile*")]:
    for line in path.read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        fields = shlex.split(line)
        sources = [field for field in fields[1:-1] if not field.startswith("--")]
        for source in sources:
            if not (root / source).exists():
                raise SystemExit(f"{path.name}: missing build input {source}")
print("Python, shell and JSON syntax and container build inputs are valid.")
