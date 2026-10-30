"""Host-only checks for opt-in graphical boot observation and recovery input."""

from pathlib import Path
import threading
import unittest
from unittest import mock

from integration import plymouth
from integration import run as harness


class GraphicalPlymouthTests(unittest.TestCase):
    def test_graphical_fixture_uses_private_qmp_and_keeps_serial_default(self):
        firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
        args = harness.parser().parse_args(["--graphical-boot", "--build", "--iso", "/tmp/integration.iso"])
        command = harness.qemu_command(args, Path("/tmp/run"), firmware, live=True)
        self.assertIn("unix:/tmp/run/plymouth-qmp.sock,server=on,wait=off", command)
        self.assertFalse(any("tcp:" in option for option in command))
        commands, _ = harness.build_commands(args, Path("/tmp/run"))
        overlays = [command for command in commands if str(harness.ROOT / "tests/integration/Containerfile") in command]
        self.assertEqual(len(overlays), 2)
        self.assertTrue(all("INTEGRATION_GRAPHICAL_BOOT=1" in command for command in overlays))
        args.graphical_boot = False
        command = harness.qemu_command(args, Path("/tmp/run"), firmware, live=True)
        self.assertNotIn("-qmp", command)
        commands, _ = harness.build_commands(args, Path("/tmp/run"))
        self.assertFalse(any("INTEGRATION_GRAPHICAL_BOOT=1" in command for command in commands))

    def test_native_splash_and_entry_are_required_before_sending_a_key(self):
        ink, bloom, canvas = (bytes.fromhex(value) for value in ("21152f", "e7ddf7", "f8f5ef"))
        header = b"P6\n200 200\n255\n"
        splash = header + ink * 38000 + bloom * 2000
        prompt = header + ink * 36000 + bloom * 2000 + canvas * 2000
        self.assertEqual(plymouth.classify_ppm(splash), (True, False))
        self.assertEqual(plymouth.classify_ppm(prompt), (True, True))
        self.assertEqual(plymouth.classify_ppm(header + b"\0\0\0" * 40000), (False, False))
        with self.assertRaises(ValueError):
            plymouth.classify_ppm(prompt[:-3])

    def test_recovery_keyboard_preserves_uppercase_and_does_not_log_the_key(self):
        observer = object.__new__(plymouth.GraphicalBoot)
        observer.key = "AB-23"
        observer.monitor = Path("/tmp/private-qmp.sock")
        observer.stop = mock.Mock()
        observer.stop.wait.return_value = False
        observer.lock = threading.Lock()
        observer.prompt_answered = False
        with mock.patch.object(plymouth, "qmp") as qmp:
            observer.type_recovery()
        self.assertTrue(observer.prompt_answered)
        self.assertIsNone(observer.key)
        calls = qmp.call_args_list
        self.assertEqual(calls[0].kwargs["keys"], [{"type": "qcode", "data": "shift"},
                                                 {"type": "qcode", "data": "a"}])
        self.assertEqual(calls[2].kwargs["keys"], [{"type": "qcode", "data": "minus"}])
        self.assertEqual(calls[-1].kwargs["keys"], [{"type": "qcode", "data": "ret"}])

    def test_blank_screen_does_not_count_as_graphical_handoff(self):
        observer = object.__new__(plymouth.GraphicalBoot)
        observer.error = None
        observer.splash_seen = True
        observer.key = None
        observer.prefix = "plymouth-live"
        observer.screenshot = mock.Mock(return_value=b"P6\n200 200\n255\n" + b"\0" * 120000)
        with mock.patch.object(plymouth.time, "monotonic", side_effect=[0, .1, 2]), \
             mock.patch.object(plymouth.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "handoff was not observed"):
                observer.verify_handoff(1)


if __name__ == "__main__":
    unittest.main()
