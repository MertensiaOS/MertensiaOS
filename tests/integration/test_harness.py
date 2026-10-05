"""Host-only checks for isolation, firmware selection and honest reporting."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location("iso_harness", Path(__file__).with_name("run.py"))
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


class HarnessTests(unittest.TestCase):
    def args(self, *arguments):
        return harness.parser().parse_args(arguments)

    def test_existing_work_directory_is_rejected_without_changing_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            disk = work / "target.qcow2"
            disk.write_bytes(b"existing disk")
            with self.assertRaises(harness.HarnessError):
                harness.make_work_dir(work)
            self.assertEqual(disk.read_bytes(), b"existing disk")

    def test_firmware_requires_secure_boot_and_pre_enrolled_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, variables = root / "CODE.fd", root / "VARS.fd"
            code.touch()
            variables.touch()
            descriptor = root / "firmware.json"
            data = {"features": ["secure-boot"], "targets": [{"architecture": "x86_64"}],
                    "mapping": {"device": "flash", "mode": "split",
                                "executable": {"filename": str(code), "format": "raw"},
                                "nvram-template": {"filename": str(variables), "format": "raw"}}}
            descriptor.write_text(json.dumps(data))
            with self.assertRaises(harness.HarnessError):
                harness.find_firmware(descriptor)
            data["features"].append("enrolled-keys")
            descriptor.write_text(json.dumps(data))
            selected = harness.find_firmware(descriptor)
            self.assertEqual(selected.code, code)
            self.assertEqual(selected.variables, variables)

    def test_explicit_firmware_rejects_nonregular_device_like_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, variables = root / "CODE.fd", root / "VARS.fd"
            code.touch()
            os.mkfifo(variables)
            with self.assertRaises(harness.HarnessError):
                harness.find_firmware(code=code, variables=variables)

    def test_qemu_attaches_only_new_file_disk_and_readonly_iso(self):
        args = self.args("--iso", "/tmp/integration.iso", "--accel", "tcg")
        firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
        command = harness.qemu_command(args, Path("/tmp/new-run"), firmware, live=True)
        drives = [command[index + 1] for index, value in enumerate(command) if value == "-drive"]
        self.assertIn("if=none,id=target,format=qcow2,file=/tmp/new-run/target.qcow2", drives)
        self.assertIn("file=/tmp/integration.iso,media=cdrom,readonly=on", drives)
        self.assertTrue(all("file=/dev/" not in value for value in drives))
        self.assertNotIn("-fsdev", command)
        self.assertNotIn("-virtfs", command)
        self.assertIn(f"virtio-blk-pci,drive=target,serial={harness.SERIAL}", command)
        self.assertIn(f"name=opt/org.mertensia/integration,string={harness.TOKEN}", command)

    def test_installed_boot_removes_cdrom_and_preserves_tpm_and_variables(self):
        args = self.args("--iso", "/tmp/integration.iso", "--accel", "tcg")
        firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
        command = harness.qemu_command(args, Path("/tmp/new-run"), firmware, live=False)
        self.assertTrue(all("media=cdrom" not in value for value in command))
        self.assertIn("socket,id=tpm,path=/tmp/new-run/swtpm.sock", command)
        self.assertIn("if=pflash,format=raw,file=/tmp/new-run/OVMF_VARS", command)

    def test_recovery_boot_exposes_console_only_through_private_unix_socket(self):
        args = self.args("--accel", "tcg")
        firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
        command = harness.qemu_command(args, Path("/tmp/new-run"), firmware, live=False, recovery_console=True)
        self.assertIn("socket,id=recovery,path=/tmp/new-run/recovery.sock,server=on,wait=off", command)
        self.assertIn("chardev:recovery", command)
        self.assertTrue(all("tcp:" not in value for value in command))

    def test_serial_recovery_prompt_matches_real_console_passphrase_requests(self):
        self.assertTrue(harness.SerialRecovery.PROMPT.search(b"Please enter passphrase for disk mertensia-root:"))
        self.assertTrue(harness.SerialRecovery.PROMPT.search(b"Password:"))
        self.assertFalse(harness.SerialRecovery.PROMPT.search(b"mertensia-integration login:"))

    def test_serial_prompt_answer_and_split_secret_echo_are_redacted(self):
        recovery = object.__new__(harness.SerialRecovery)
        recovery.key = b"private-root-recovery"
        recovery.socket = mock.Mock()
        recovery.socket.recv.side_effect = [b"Please enter pass", b"phrase for disk root: ",
                                           b"private-root-", b"recovery\nBooted\n", b""]
        recovery.log = io.BytesIO()
        recovery.stop = mock.Mock()
        recovery.stop.is_set.return_value = False
        recovery.prompt_answered = False
        recovery.read()
        recovery.socket.sendall.assert_called_once_with(b"private-root-recovery\n")
        self.assertTrue(recovery.prompt_answered)
        self.assertNotIn(recovery.key, recovery.log.getvalue())
        self.assertIn(b"[redacted]", recovery.log.getvalue())

    def test_qemu_option_delimiters_in_paths_are_rejected(self):
        with self.assertRaises(harness.HarnessError):
            harness.safe_option_path(Path("/tmp/a,unsafe"))

    def test_build_uses_production_files_and_separate_test_overlays(self):
        args = self.args("--build")
        commands, images = harness.build_commands(args, Path("/tmp/new-run"))
        self.assertEqual(commands[0][commands[0].index("-f") + 1], str(harness.ROOT / "Containerfile"))
        self.assertTrue(any(str(harness.ROOT / "Containerfile.installer") in command for command in commands))
        self.assertTrue(any("BUILD_MODE=development" in command for command in commands))
        self.assertTrue(any("INTEGRATION_ROLE=payload" in command for command in commands))
        self.assertTrue(any("INTEGRATION_ROLE=live" in command for command in commands))
        self.assertIn("bootc-generic-iso", commands[-1])
        self.assertIn(images["payload"], commands[-1])
        self.assertTrue(all("push" not in command for command in commands))
        self.assertTrue(all("Containerfile.dev" not in value for command in commands for value in command))

    def test_reports_record_failures_and_explicitly_skip_unrun_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = harness.Results(root, root / "junit.xml")
            results.step("boot", lambda: {"secure_boot": True})
            results.skip("upgrade", "no signed update image supplied")
            with self.assertRaises(harness.HarnessError):
                results.step("login", lambda: (_ for _ in ()).throw(harness.HarnessError("PAM failed")))
            report = json.loads((root / "result.json").read_text())
            self.assertFalse(report["passed"])
            self.assertEqual([step["status"] for step in report["steps"]], ["passed", "skipped", "failed"])
            xml = ET.parse(root / "junit.xml").getroot()
            self.assertEqual(xml.attrib["failures"], "1")
            self.assertEqual(xml.attrib["skipped"], "1")

    def test_reports_never_store_passwords_or_recovery_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = harness.Results(root, root / "junit.xml")
            results.secrets.extend(["secret-password", "secret-recovery-key"])
            results.step("check", lambda: {"password": "secret-password", "recovery_key": "secret-recovery-key",
                                           "message": "secret-password secret-recovery-key"})
            text = (root / "result.json").read_text() + (root / "junit.xml").read_text()
            self.assertNotIn("secret-password", text)
            self.assertNotIn("secret-recovery-key", text)
            self.assertIn("[redacted]", text)

    def test_interrupted_or_partial_run_is_never_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = harness.Results(root)
            results.step("boot", lambda: {"secure_boot": True})
            report = json.loads((root / "result.json").read_text())
            self.assertFalse(report["complete"])
            self.assertFalse(report["passed"])
            results.complete = True
            results.save()
            self.assertTrue(json.loads((root / "result.json").read_text())["passed"])

    def test_junit_cannot_escape_work_directory_or_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            outside = root / "outside.xml"
            outside.write_text("preserve me")
            with self.assertRaises(harness.HarnessError):
                harness.Results(work, outside)
            link = work / "report.xml"
            link.symlink_to(outside)
            with self.assertRaises(harness.HarnessError):
                harness.Results(work, link)
            self.assertEqual(outside.read_text(), "preserve me")

    def test_junit_rejects_nonregular_report_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "report.xml"
            os.mkfifo(fifo)
            with self.assertRaises(harness.HarnessError):
                harness.Results(root, fifo)

    def test_protocol_secrets_stay_in_memory_and_progress_is_sanitized(self):
        guest = object.__new__(harness.Guest)
        guest.args = self.args()
        guest.socket = mock.Mock()
        guest.stream = mock.Mock()
        guest.counter = 0
        guest.secret = None
        known_secrets = []
        guest.on_secret = known_secrets.append
        guest.redact = lambda value: value.replace("private-key", "[redacted]")
        guest.receive = mock.Mock(side_effect=[
            {"type": "secret", "recovery_key": "private-key"},
            {"type": "progress", "event": {"event": "phase", "id": "finalize"}},
            {"type": "result", "id": 1, "ok": True, "result": {"installed": True}},
        ])
        with mock.patch("builtins.print") as printed:
            self.assertEqual(guest.command("install"), {"installed": True})
        self.assertEqual(guest.secret, "private-key")
        self.assertEqual(known_secrets, ["private-key"])
        self.assertNotIn("private-key", str(printed.call_args_list))

    def test_final_cli_errors_redact_guest_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso = root / "integration.iso"
            iso.touch()
            firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))

            def fail(_args, _work, _firmware, results):
                results.secrets.append("private-test-key")
                raise harness.HarnessError("operation failed with private-test-key")

            with mock.patch.object(harness, "capabilities", return_value={"ok": True}), \
                 mock.patch.object(harness, "find_firmware", return_value=firmware), \
                 mock.patch.object(harness, "run_suite", side_effect=fail), mock.patch("builtins.print") as printed:
                self.assertEqual(harness.main(["--iso", str(iso), "--work-dir", str(root / "new-run")]), 1)
            self.assertNotIn("private-test-key", str(printed.call_args_list))
            self.assertIn("[redacted]", str(printed.call_args_list))

    def test_failed_tpm_startup_kills_stuck_process_and_closes_log(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            process = mock.Mock()
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("swtpm", 20), 0]
            firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
            with mock.patch.object(harness.subprocess, "run"), mock.patch.object(harness.shutil, "copyfile"), \
                 mock.patch.object(harness.subprocess, "Popen", return_value=process) as launched, \
                 mock.patch.object(harness, "wait_path", side_effect=harness.HarnessError("startup failed")):
                with self.assertRaises(harness.HarnessError):
                    harness.run_suite(self.args(), work, firmware, harness.Results(work))
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertTrue(launched.call_args.kwargs["stdout"].closed)


class TestSigningPolicyTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("integration_policy", Path(__file__).with_name("add_test_policy.py"))
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)

    def test_narrow_test_scope_keeps_production_trust_and_default_reject(self):
        original = {"default": [{"type": "reject"}], "transports": {"docker": {
            "ghcr.io/mertensiaos/mertensiaos": [{"type": "sigstoreSigned", "keyPath": "/production.pub"}]}}}
        updated = self.policy.add_scope(original, "ghcr.io/example/mertensia-integration:next")
        self.assertEqual(updated["default"], [{"type": "reject"}])
        self.assertEqual(updated["transports"]["docker"]["ghcr.io/mertensiaos/mertensiaos"][0]["keyPath"], "/production.pub")
        self.assertEqual(updated["transports"]["docker"]["ghcr.io/example/mertensia-integration"][0]["signedIdentity"], {"type": "matchRepository"})

    def test_test_key_cannot_replace_production_or_existing_trust(self):
        with self.assertRaises(ValueError):
            self.policy.add_scope({"default": [{"type": "reject"}]}, "ghcr.io/mertensiaos/mertensiaos:latest")
        with self.assertRaises(ValueError):
            self.policy.add_scope({"default": [{"type": "insecureAcceptAnything"}]}, "ghcr.io/example/integration:next")
        with self.assertRaises(ValueError):
            self.policy.add_scope({"default": [{"type": "reject"}], "transports": {"docker": {"ghcr.io/example/integration": []}}},
                                  "ghcr.io/example/integration:next")


if __name__ == "__main__":
    unittest.main()
