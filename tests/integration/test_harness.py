"""Host-only checks for isolation, firmware selection and honest reporting."""

import io
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


from integration import add_test_policy, guest, run as harness



class HarnessTests(unittest.TestCase):
    def args(self, *arguments):
        return harness.parser().parse_args(arguments)

    def test_automatic_update_requires_a_tag_in_the_trusted_fixture_repository(self):
        image = "ghcr.io/example/mertensia-integration"
        for arguments in (
            ["--automatic-update-image", image + ":next"],
            ["--upgrade-image", image + ":next", "--automatic-update-image", image + "@sha256:" + "a" * 64],
            ["--upgrade-image", image + ":next", "--automatic-update-image", "ghcr.io/example/other-integration:next"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as raised:
                harness.main(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_automatic_update_restores_test_origin_when_staging_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment_root = root / "deployments"
            origin = deployment_root / "default/deploy/abcd.0.origin"
            origin.parent.mkdir(parents=True)
            original = "[origin]\ncontainer-image-reference=ostree-unverified-registry:localhost/test:base\n"
            origin.write_text(original)
            policy = root / "policy.json"
            repository = "ghcr.io/example/mertensia-integration"
            policy.write_text(json.dumps({"transports": {"docker": {repository: [{"type": "sigstoreSigned"}]}}}))
            boot_id = root / "boot-id"
            boot_id.write_text("boot-a")
            actual_path = Path

            def path(value):
                return {"/sysroot/ostree/deploy": deployment_root, "/etc/containers/policy.json": policy,
                        "/proc/sys/kernel/random/boot_id": boot_id}.get(str(value), actual_path(value))

            def run(args, **kwargs):
                if args[-1] == "bootc-fetch-apply-updates.service":
                    self.assertIn("ostree-image-signed:docker://" + repository + ":next", origin.read_text())
                    raise RuntimeError("download failed")
                return ""

            identity = {"digest": "original", "status": {"status": {"booted": {
                "ostree": {"stateroot": "default", "checksum": "abcd", "deploySerial": 0}}}}}
            with mock.patch.object(guest, "Path", side_effect=path), \
                 mock.patch.object(guest, "role", return_value="payload"), \
                 mock.patch.object(guest, "boot_health", return_value={"healthy": True}), \
                 mock.patch.object(guest, "boot_identity", return_value=identity), \
                 mock.patch.object(guest, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "download failed"):
                    guest.automatic_upgrade(repository + ":next")
            self.assertEqual(origin.read_text(), original)

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
        installer = next(command for command in commands if str(harness.ROOT / "Containerfile.installer") in command)
        self.assertIn(f"BASE_IMAGE={images['base']}", installer)
        self.assertIn(f"SOURCE_IMAGE={images['payload']}", installer)

    def test_live_test_boot_retains_install_requirements_and_exposes_serial_diagnostics(self):
        def kernel_arguments(path):
            text = path.read_text()
            line = re.search(r'^\s*linux:\s*"([^"]+)"', text, re.MULTILINE)
            self.assertIsNotNone(line)
            return shlex.split(line.group(1))

        production = kernel_arguments(harness.ROOT / "installer/config/iso.yaml")
        integration = kernel_arguments(Path(__file__).with_name("iso.yaml"))
        for argument in production:
            if argument not in {"quiet", "rhgb"}:
                self.assertIn(argument, integration)
        self.assertNotIn("quiet", integration)
        self.assertNotIn("rhgb", integration)
        self.assertIn("console=ttyS0,115200n8", integration)
        self.assertIn("rd.plymouth=0", integration)

    def test_startup_failure_is_reported_without_waiting_for_boot_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            process = mock.Mock()
            process.poll.return_value = 0
            firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
            with mock.patch.object(harness.subprocess, "Popen", return_value=process), \
                 mock.patch.object(harness, "wait_path"), mock.patch.object(harness.socket, "socket"), \
                 mock.patch.object(harness.Guest, "receive", return_value={"type": "startup-error", "protocol": 1,
                                                                         "error": "test disk serial does not match"}):
                with self.assertRaisesRegex(harness.HarnessError, "integration guest startup failed: test disk serial"):
                    harness.Guest(self.args("--iso", "/tmp/integration.iso"), work, firmware, True)

    def test_boot_timeout_identifies_stage_process_state_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            process = mock.Mock()
            process.poll.return_value = None
            firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
            with mock.patch.object(harness.subprocess, "Popen", return_value=process), \
                 mock.patch.object(harness, "wait_path"), mock.patch.object(harness.socket, "socket"), \
                 mock.patch.object(harness.Guest, "receive", side_effect=socket.timeout()):
                with self.assertRaises(harness.HarnessError) as raised:
                    harness.Guest(self.args("--iso", "/tmp/integration.iso", "--boot-timeout", "2"), work, firmware, True)
            message = str(raised.exception)
            self.assertIn("after 2s", message)
            self.assertIn("live ISO", message)
            self.assertIn("QEMU is still running", message)
            self.assertIn("live.serial.log", message)
            self.assertIn("qemu-live.log", message)

    def test_sudo_handoff_changes_reports_and_directory_but_not_private_tpm_state(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            report, log = work / "result.json", work / "build.log"
            report.write_text("{}")
            log.write_text("build log")
            state = work / "tpm"
            state.mkdir()
            private = state / "private-state"
            private.write_text("private")
            changed = []

            def record(descriptor, uid, gid):
                changed.append((os.fstat(descriptor).st_ino, uid, gid))

            with mock.patch.object(harness.os, "geteuid", return_value=0), \
                 mock.patch.dict(harness.os.environ, {"SUDO_UID": "1001", "SUDO_GID": "1002"}), \
                 mock.patch.object(harness.os, "fchown", side_effect=record), mock.patch.object(harness.os, "fchmod"):
                harness.handoff_diagnostics(work)
            self.assertEqual({inode for inode, _uid, _gid in changed}, {work.stat().st_ino, report.stat().st_ino, log.stat().st_ino})
            self.assertTrue(all((uid, gid) == (1001, 1002) for _inode, uid, gid in changed))
            self.assertNotIn(private.stat().st_ino, {inode for inode, _uid, _gid in changed})

    def test_nonroot_caller_cannot_trigger_handoff_using_forged_sudo_environment(self):
        with mock.patch.object(harness.os, "geteuid", return_value=1000), \
             mock.patch.dict(harness.os.environ, {"SUDO_UID": "1001", "SUDO_GID": "1002"}), \
             mock.patch.object(harness.os, "fchown") as changed:
            harness.handoff_diagnostics(Path("/tmp/not-opened"))
        changed.assert_not_called()

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

    def test_each_boot_restarts_swtpm_and_preserves_the_selected_state(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            processes = []
            state_directories = []

            def start_tpm(argv, **kwargs):
                state = Path(argv[argv.index("--tpmstate") + 1].removeprefix("dir="))
                marker = state / "enrolled-key"
                if state in state_directories:
                    self.assertEqual(marker.read_text(), "persistent TPM identity")
                else:
                    marker.write_text("persistent TPM identity")
                state_directories.append(state)
                process = mock.Mock()
                process.poll.return_value = None
                processes.append(process)
                return process

            def boot_guest(*args, **kwargs):
                process = processes[-1]
                self.assertIsNone(process.poll(), "QEMU must not reuse a shut-down swtpm process")
                instance = mock.Mock()
                instance.secret = "private-test-recovery-key"
                instance.command.return_value = {"digest": "original-deployment"}
                instance.serial.prompt_answered = True
                # QEMU's normal shutdown also shuts down swtpm, removing its
                # control socket. The next boot must start a fresh emulator.
                instance.poweroff.side_effect = lambda: setattr(process.poll, "return_value", 0)
                return instance

            firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
            with mock.patch.object(harness.subprocess, "run"), \
                 mock.patch.object(harness.shutil, "copyfile"), \
                 mock.patch.object(harness.subprocess, "Popen", side_effect=start_tpm), \
                 mock.patch.object(harness, "wait_path"), \
                 mock.patch.object(harness, "Guest", side_effect=boot_guest), \
                 mock.patch("builtins.print"):
                harness.run_suite(self.args(), work, firmware, harness.Results(work))
            self.assertEqual(state_directories, [work / name for name in (
                "tpm", "tpm", "tpm", "tpm-recovery", "tpm",
            )])
            self.assertTrue(all(process.poll() == 0 for process in processes))


class GreenbootRecoveryTests(unittest.TestCase):
    def test_follows_expected_reboots_then_checks_restored_digest(self):
        ready = mock.Mock()
        ready.command.return_value = {"healthy": True, "digest": "original"}
        boot = mock.Mock(side_effect=[harness.ExpectedReboot("retry"), harness.ExpectedReboot("retry"), ready])
        instance, result = harness.follow_greenboot(boot, "original")
        self.assertIs(instance, ready)
        self.assertEqual(result["automatic_reboots"], 2)
        ready.close.assert_not_called()

    def test_running_unhealthy_guest_is_never_force_rebooted(self):
        stopped = mock.Mock()
        stopped.command.return_value = {"healthy": False, "digest": "original", "manual_intervention": True}
        stopped.wait_for_reboot.return_value = False
        instance, summary = harness.follow_greenboot(lambda: stopped, "original", terminal_unhealthy=True)
        self.assertIs(instance, stopped)
        self.assertTrue(summary["manual_intervention"])
        stopped.close.assert_not_called()

    def test_automatic_reboot_budget_is_enforced(self):
        boot = mock.Mock(side_effect=harness.ExpectedReboot("retry"))
        with self.assertRaisesRegex(harness.HarnessError, "3-reboot recovery limit"):
            harness.follow_greenboot(boot, "original", max_reboots=3)
        self.assertEqual(boot.call_count, 4)

    def test_wrong_digest_and_unexpected_success_fail_acceptance(self):
        for terminal, digest in ((False, "wrong"), (True, "original")):
            instance = mock.Mock()
            instance.command.return_value = {"healthy": True, "digest": digest}
            with self.assertRaises(harness.HarnessError):
                harness.follow_greenboot(lambda: instance, "original", terminal_unhealthy=terminal)
            instance.close.assert_called_once()

    def test_only_explicit_recovery_channel_closures_can_mean_reboot(self):
        for expected in (True, False):
            instance = object.__new__(harness.Guest)
            instance.stream = mock.Mock()
            instance.stream.readline.return_value = b""
            instance.expect_reboot = expected
            instance.wait_for_reboot = mock.Mock(return_value=True)
            exception = harness.ExpectedReboot if expected else harness.HarnessError
            with self.assertRaises(exception):
                instance.receive()
            self.assertEqual(instance.wait_for_reboot.call_count, int(expected))

    def test_unhealthy_guest_without_diagnostic_fails_instead_of_passing(self):
        instance = mock.Mock()
        instance.command.return_value = {"healthy": False, "digest": "original", "manual_intervention": False}
        instance.wait_for_reboot.return_value = False
        with self.assertRaisesRegex(harness.HarnessError, "manual-intervention diagnostic"):
            harness.follow_greenboot(lambda: instance, "original", terminal_unhealthy=True)

    def test_recovery_options_require_distinct_signed_fixtures_in_one_repository(self):
        base = "ghcr.io/example/mertensia-integration:"
        invalid = [
            ["--require-greenboot-recovery"],
            ["--greenboot-failure-image", base + "bad"],
            ["--upgrade-image", base + "good", "--greenboot-failure-image", base + "good"],
            ["--upgrade-image", base + "good", "--greenboot-failure-image", "ghcr.io/other/another-integration:bad"],
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), mock.patch("sys.stderr", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit) as error:
                harness.main(arguments + ["--check"])
            self.assertEqual(error.exception.code, 2)

    def test_transient_fixture_fails_once_and_is_guarded(self):
        original = Path.read_text
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)

            def read(path, *args, **kwargs):
                if path.name == "health-mode":
                    return "transient"
                if path.name == "revision":
                    return "test-revision"
                return original(path, *args, **kwargs)

            with mock.patch.object(guest, "guard_guest") as guard, \
                 mock.patch.object(guest, "role", return_value="payload"), \
                 mock.patch.object(guest, "TEST_STATE", state), \
                 mock.patch.object(Path, "read_text", read):
                self.assertEqual(guest.health_fixture(), 1)
                self.assertEqual(guest.health_fixture(), 0)
            self.assertEqual(guard.call_count, 2)

    def test_numbered_boot_diagnostics_never_overwrite_an_earlier_boot(self):
        args = harness.parser().parse_args([])
        firmware = harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd"))
        first = harness.qemu_command(args, Path("/tmp/run"), firmware, live=False, boot_label="001")
        second = harness.qemu_command(args, Path("/tmp/run"), firmware, live=False, boot_label="002")
        self.assertIn("file:/tmp/run/installed-001.serial.log", first)
        self.assertIn("file:/tmp/run/installed-002.serial.log", second)
        self.assertIn("-no-reboot", first)

    def test_boot_mount_mode_is_restored_even_when_fixture_write_fails(self):
        with mock.patch.object(guest, "run", side_effect=["ro,relatime", "", ""]) as run:
            with self.assertRaises(OSError):
                with guest.writable_boot():
                    raise OSError("injected write failure")
        self.assertEqual(run.call_args_list[-1].args[0], ["mount", "-o", "remount,ro", "/boot"])

    def test_failed_fixture_cannot_run_outside_the_guarded_guest(self):
        with mock.patch.object(guest, "guard_guest", side_effect=RuntimeError("not a disposable guest")), \
             mock.patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(RuntimeError, "not a disposable guest"):
                guest.health_fixture()
        read.assert_not_called()


class TestSigningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = add_test_policy

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


class TPMUnlockResultTests(unittest.TestCase):
    def test_original_tpm_requires_success(self):
        for code in (0, 1, 2, 4, -9):
            with self.subTest(code=code):
                result = SimpleNamespace(returncode=code, stderr="TPM key integrity check failed")
                if code == 0:
                    guest.check_tpm_unlock(result, expect_tpm=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, "TPM unlock returned"):
                        guest.check_tpm_unlock(result, expect_tpm=True)

    def test_replacement_tpm_accepts_only_known_key_rejection_diagnostics(self):
        for code in (1, 2):
            for diagnostic in (
                "Failed to unseal secret using TPM2: State not recoverable",
                "TPM key integrity check failed. Key enrolled in superblock most likely does not belong to this TPM.",
            ):
                with self.subTest(code=code, diagnostic=diagnostic):
                    guest.check_tpm_unlock(SimpleNamespace(returncode=code, stderr=diagnostic), expect_tpm=False)

    def test_operational_errors_and_unexpected_success_do_not_prove_rejection(self):
        for code, diagnostic in (
            (0, ""),
            (1, "Failed to create TPM2 context: State not recoverable"),
            (1, "Failed to load external token plugin systemd-tpm2"),
            (1, "Unknown option"),
            (2, "Permission denied"),
            (2, "No key available with this token"),
            (4, "TPM key integrity check failed"),
            (-9, "Failed to unseal secret using TPM2: State not recoverable"),
        ):
            with self.subTest(code=code, diagnostic=diagnostic), self.assertRaisesRegex(RuntimeError, "Expected TPM key rejection"):
                guest.check_tpm_unlock(SimpleNamespace(returncode=code, stderr=diagnostic), expect_tpm=False)


class GuestGuardContractTests(unittest.TestCase):
    def setUp(self):
        self.guest = guest

    def hardware_serial(self):
        args = harness.parser().parse_args(["--accel", "tcg"])
        command = harness.qemu_command(args, Path("/tmp/new-run"), harness.Firmware(Path("/tmp/CODE.fd"), Path("/tmp/VARS.fd")), live=False)
        device = next(value for value in command if value.startswith("virtio-blk-pci,"))
        serial = device.split("serial=", 1)[1]
        # The Linux virtio block ABI exposes VIRTIO_BLK_ID_BYTES=20; model the
        # actual device truncation, not a comparison of two local constants.
        self.assertLessEqual(len(serial.encode()), 20)
        return serial.encode()[:20].decode()

    def test_guest_guard_accepts_the_serial_exposed_by_the_host_transport(self):
        disk = json.dumps({"blockdevices": [{"path": "/dev/vda", "type": "disk", "ro": False}]})
        with mock.patch.object(self.guest.os, "geteuid", return_value=0), \
             mock.patch.object(self.guest, "GUARD") as guard, \
             mock.patch.object(self.guest, "run", side_effect=["qemu", self.hardware_serial(), disk]):
            guard.read_bytes.return_value = self.guest.TOKEN
            self.guest.guard_guest()

    def test_guest_guard_rejects_a_mismatched_disk_serial(self):
        with mock.patch.object(self.guest.os, "geteuid", return_value=0), \
             mock.patch.object(self.guest, "GUARD") as guard, \
             mock.patch.object(self.guest, "run", side_effect=["qemu", "UNRELATED-DISK"]):
            guard.read_bytes.return_value = self.guest.TOKEN
            with self.assertRaisesRegex(RuntimeError, "test disk serial does not match"):
                self.guest.guard_guest()

    def test_guest_guard_rejects_an_additional_writable_disk(self):
        disks = json.dumps({"blockdevices": [{"path": path, "type": "disk", "ro": False} for path in ("/dev/vda", "/dev/vdb")]})
        with mock.patch.object(self.guest.os, "geteuid", return_value=0), \
             mock.patch.object(self.guest, "GUARD") as guard, \
             mock.patch.object(self.guest, "run", side_effect=["qemu", self.hardware_serial(), disks]):
            guard.read_bytes.return_value = self.guest.TOKEN
            with self.assertRaisesRegex(RuntimeError, "exactly one writable disk"):
                self.guest.guard_guest()


if __name__ == "__main__":
    unittest.main()
