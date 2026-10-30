"""Exercise cache invalidation with real image files and checksum failures."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("iso_cache", ROOT / "scripts/iso-cache.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


class IsoCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.image = Path(temporary.name) / "installer.iso"
        self.image.write_bytes(b"ISO contents")
        self.inputs = ["payload-id", "installer-id", "builder-id", "recipe-hash"]

    def run_cache(self, action, inputs=None):
        return cache.main([action, str(self.image), *(self.inputs if inputs is None else inputs)])

    def test_reuses_only_matching_inputs_and_complete_contents(self):
        self.assertEqual(self.run_cache("check"), 1)
        self.assertEqual(self.run_cache("record"), 0)
        self.assertEqual(self.run_cache("check"), 0)
        for index in range(len(self.inputs)):
            changed = self.inputs.copy()
            changed[index] += "-changed"
            self.assertEqual(self.run_cache("check", changed), 1)
        self.image.write_bytes(b"ISO contEnts")
        self.assertEqual(self.run_cache("check"), 1)

    def test_missing_or_corrupt_state_and_deleted_iso_rebuild(self):
        state = self.image.with_suffix(".iso-cache.json")
        for content in ("{", "[]", "null", '{"inputs": "wrong"}'):
            state.write_text(content)
            self.assertEqual(self.run_cache("check"), 1)
        self.run_cache("record")
        self.image.unlink()
        self.assertEqual(self.run_cache("check"), 1)

    def test_failed_rebuild_cannot_reuse_previous_success(self):
        self.run_cache("record")
        self.run_cache("invalidate")
        self.assertEqual(self.run_cache("check"), 1)
        self.image.write_bytes(b"")
        with self.assertRaisesRegex(SystemExit, "nonempty ISO"):
            self.run_cache("record")
        self.assertEqual(self.run_cache("check"), 1)
