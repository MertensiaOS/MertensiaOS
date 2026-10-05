#!/usr/bin/env python3
"""Run every unit test; a missing desktop dependency must fail CI, not skip it."""
import pathlib
import sys
import unittest

root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print("Required tests were skipped; install the documented test dependencies.", file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
