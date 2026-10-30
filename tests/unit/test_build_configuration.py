"""A failed configuration block must stop the image build immediately."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BuildConfigurationTests(unittest.TestCase):
    def test_configuration_failures_do_not_continue_to_later_blocks(self):
        for role, first, following in (("payload", "chmod", "find"),
                                       ("installer", "rm", "systemctl")):
            with self.subTest(role=role):
                # Stop at the next operation even if the failure boundary is
                # broken, so this host-only test never configures the host OS.
                command = (f'{first}() {{ return 37; }}; '
                           f'{following}() {{ echo unexpected-continuation; exit 42; }}; '
                           f'export -f {first} {following}; bash "$1"')
                result = subprocess.run(
                    ["bash", "-c", command, "test", str(ROOT / f"scripts/configure-{role}.sh")],
                    text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
                self.assertNotIn("unexpected-continuation", result.stdout)
