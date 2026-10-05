import importlib.util
import io
import stat
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "installer/mertensia_installer_backend.py"
SPEC = importlib.util.spec_from_file_location("installer_backend", MODULE_PATH)
backend = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(backend)


class InstallerBackendTests(unittest.TestCase):
    def disk(self):
        return {"path": "/dev/test", "stable_path": "/dev/test", "device_id": 123,
                "size": 40 * 1024**3, "serial": "test"}

    @contextmanager
    def retained_state(self, phase="installed"):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(backend, "STATE_ROOT", Path(directory) / "state"))
            work, state = backend._new_state(self.disk(), "/dev/test3")
            state["phase"] = phase
            backend._save_state(work, state)
            original_stat = backend.os.stat

            def device_stat(path, *args, **kwargs):
                if str(path) == "/dev/test":
                    return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=123)
                return original_stat(path, *args, **kwargs)

            stack.enter_context(mock.patch.object(backend.os, "stat", side_effect=device_stat))
            stack.enter_context(mock.patch.object(backend, "_assert_disk_identity"))
            stack.enter_context(mock.patch.object(backend, "_partition_path", return_value="/dev/test3"))
            yield work, state, stack

    def test_disk_list_excludes_live_media_and_read_only_disks(self):
        devices = [
            {"path": "/dev/live", "type": "disk", "size": 40 * 1024**3},
            {"path": "/dev/parent", "type": "disk", "size": 40 * 1024**3,
             "children": [{"path": "/dev/live-partition", "type": "part"}]},
            {"path": "/dev/readonly", "type": "disk", "ro": True,
             "size": 40 * 1024**3},
            {"path": "/dev/target", "type": "disk", "size": 40 * 1024**3,
             "model": " Test disk ", "serial": " serial-1 "},
        ]
        with mock.patch.object(backend, "run", return_value=SimpleNamespace(
            stdout=backend.json.dumps({"blockdevices": devices})
        )), mock.patch.object(backend, "_disk_busy_reason", return_value=None), mock.patch.object(backend, "_live_backing_devices", return_value={
            "/dev/live", "/dev/live-partition"
        }):
            disks = backend.list_disks()
        self.assertEqual([disk["path"] for disk in disks], ["/dev/target"])
        self.assertEqual(disks[0]["model"], "Test disk")
        self.assertEqual(disks[0]["serial"], "serial-1")

    def test_selected_disk_must_be_large_and_a_block_device(self):
        disk = {"path": "/dev/test-disk", "stable_path": "/dev/test-disk",
                "size": backend.MINIMUM_DISK_BYTES}
        with mock.patch.object(backend, "list_disks", return_value=[disk]), mock.patch.object(
            backend.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)
        ), self.assertRaisesRegex(backend.InstallError, "not a block device"):
            backend.resolve_selected_disk("/dev/test-disk")
        disk["size"] -= 1
        with mock.patch.object(backend, "list_disks", return_value=[disk]), self.assertRaisesRegex(
            backend.InstallError, "at least 24 GiB"
        ):
            backend.resolve_selected_disk("/dev/test-disk")

    def test_disk_list_excludes_small_mounted_swap_and_held_disks(self):
        def disk(name, **values):
            return {"path": "/dev/" + name, "kname": name, "type": "disk",
                    "size": 40 * 1024**3, **values}
        devices = [disk("small", size=1024),
                   disk("mounted", children=[{"path": "/dev/mounted1", "type": "part", "mountpoints": ["/mnt/files"]}]),
                   disk("swap", children=[{"path": "/dev/swap1", "type": "part"}]),
                   disk("held"), disk("mapped", children=[{"path": "/dev/dm-0", "type": "crypt"}]),
                   disk("idle")]
        with mock.patch.object(backend, "_block_devices", return_value=devices), mock.patch.object(
            backend, "_live_backing_devices", return_value=set()
        ), mock.patch.object(backend, "_swap_devices", return_value={"/dev/swap1"}), mock.patch.object(
            backend, "_device_holders", side_effect=lambda node: ["dm-0"] if node["path"] == "/dev/held" else []
        ):
            self.assertEqual([disk["path"] for disk in backend.list_disks()], ["/dev/idle"])

    def test_missing_holder_inventory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, "SYS_BLOCK", Path(directory)):
            with self.assertRaises(OSError):
                backend._disk_busy_reason({"path": "/dev/unavailable", "type": "disk"}, set())

    def test_pre_erase_check_rejects_mounts_swap_and_holders(self):
        for kind in ("mount", "swap", "holder", "inspection"):
            node = {"path": "/dev/test", "type": "disk", "mountpoints": ["/mnt/files"] if kind == "mount" else []}
            with self.subTest(kind=kind), mock.patch.object(backend, "_assert_disk_identity", return_value=node), mock.patch.object(
                backend, "_live_backing_devices", return_value=set()
            ), mock.patch.object(backend, "_swap_devices", return_value={"/dev/test"} if kind == "swap" else set()), mock.patch.object(
                backend, "_device_holders", return_value=["dm-0"] if kind == "holder" else [],
                side_effect=OSError("sysfs unavailable") if kind == "inspection" else None,
            ):
                with self.assertRaises((backend.InstallError, OSError)):
                    backend._assert_disk_idle(self.disk())

    def test_identity_changes_are_rejected_before_erasure(self):
        for changed in ("device", "size", "serial", "read-only", "stable-path"):
            disk = self.disk()
            node = {"path": disk["path"], "type": "disk", "size": disk["size"], "serial": disk["serial"], "ro": False}
            if changed == "size":
                node["size"] -= 1
            elif changed == "serial":
                node["serial"] = "replacement"
            elif changed == "read-only":
                node["ro"] = True
            elif changed == "stable-path":
                disk["stable_path"] = "/dev/replacement"
            with self.subTest(changed=changed), mock.patch.object(
                backend.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=999 if changed == "device" else 123)
            ), mock.patch.object(backend, "_block_devices", return_value=[node]):
                with self.assertRaises(backend.InstallError):
                    backend._assert_disk_identity(disk)

    def test_busy_disk_and_production_fetch_failure_never_erase(self):
        for failure in ("busy", "fetch"):
            config = {"SOURCE_IMAGE": "source", "TARGET_IMAGE": "target", "BUILD_MODE": "production"}
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                for name, value in {"requirements": {"ok": True}, "resolve_selected_disk": self.disk(),
                                    "load_config": config, "_check_scratch": None}.items():
                    stack.enter_context(mock.patch.object(backend, name, return_value=value))
                stack.enter_context(mock.patch.object(backend, "STATE_ROOT", Path(directory) / "state"))
                stack.enter_context(mock.patch.object(backend, "emit"))
                check = stack.enter_context(mock.patch.object(backend, "_assert_disk_idle", side_effect=backend.InstallError("disk busy")))
                commands = stack.enter_context(mock.patch.object(backend, "run", side_effect=backend.InstallError("fetch failed") if failure == "fetch" else None))
                with self.assertRaises(backend.InstallError):
                    backend.install("/dev/test")
                self.assertFalse(any(call.args[0][0] in {"wipefs", "sgdisk", "cryptsetup"} for call in commands.call_args_list))
                if failure == "fetch":
                    check.assert_not_called()

    def test_previous_mapping_or_target_mount_blocks_new_disk_before_erasure(self):
        for busy in ("mapping", "mount", "submount", "symlink"):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                target = Path(directory) / "target"
                if busy == "symlink":
                    target.symlink_to(Path(directory))
                for name, value in {"requirements": {"ok": True}, "resolve_selected_disk": self.disk(),
                                    "load_config": {"SOURCE_IMAGE": "test", "TARGET_IMAGE": "test"},
                                    "_check_scratch": None, "_assert_disk_idle": None}.items():
                    stack.enter_context(mock.patch.object(backend, name, return_value=value))
                stack.enter_context(mock.patch.object(backend, "STATE_ROOT", Path(directory) / "state"))
                stack.enter_context(mock.patch.object(backend, "MOUNTPOINT", target))
                stack.enter_context(mock.patch.object(backend.os.path, "lexists", return_value=busy == "mapping"))
                mounts = [target] if busy == "mount" else [target / "boot"] if busy == "submount" else []
                stack.enter_context(mock.patch.object(backend, "_mount_targets", return_value=mounts))
                stack.enter_context(mock.patch.object(backend, "emit"))
                commands = stack.enter_context(mock.patch.object(backend, "run"))
                with self.assertRaises(backend.InstallError):
                    backend.install("/dev/test")
                commands.assert_not_called()

    def test_prepared_format_failure_explains_safe_disposition(self):
        for status in ("absent", "matching", "unknown"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                for name, value in {"requirements": {"ok": True}, "resolve_selected_disk": self.disk(),
                                    "load_config": {"SOURCE_IMAGE": "test", "TARGET_IMAGE": "test"},
                                    "_check_scratch": None, "_assert_disk_idle": None, "_assert_workspace_idle": None,
                                    "_prepared_volume_status": status}.items():
                    stack.enter_context(mock.patch.object(backend, name, return_value=value))
                state_root = Path(directory) / "state"
                stack.enter_context(mock.patch.object(backend, "STATE_ROOT", state_root))
                stack.enter_context(mock.patch.object(backend, "_partition_path", side_effect=lambda disk, n: f"{disk}{n}"))
                stack.enter_context(mock.patch.object(backend, "emit"))

                def command(argv, **kwargs):
                    if argv[:2] == ["cryptsetup", "luksFormat"]:
                        raise backend.InstallError("format failed")
                    return SimpleNamespace(stdout="")

                stack.enter_context(mock.patch.object(backend, "run", side_effect=command))
                with self.assertRaisesRegex(backend.InstallError, "format failed") as error:
                    backend.install("/dev/test")
                work = state_root / "disk-123"
                self.assertTrue((work / "setup.key").exists())
                self.assertEqual(backend.json.loads((work / "state.json").read_text())["phase"], "prepared")
                if status == "absent":
                    self.assertIn("discard-prepared --disk /dev/test", str(error.exception))
                    self.assertIn("restarting the live system is also safe", str(error.exception))
                else:
                    self.assertIn("Do not restart", str(error.exception))
                    self.assertIn("recover --disk /dev/test", str(error.exception))

    def test_discard_prepared_requires_error_free_absence_probes(self):
        for condition in ("absent", "matching", "mismatched", "uuid-error", "isLuks-error", "blkid-error", "read-error", "signature"):
            with self.subTest(condition=condition), self.retained_state("prepared") as (work, state, stack):
                stack.enter_context(mock.patch.object(backend, "_assert_workspace_idle"))
                probes = []

                def probe(argv, **kwargs):
                    probes.append(argv)
                    if argv[1] == "luksUUID":
                        if condition in {"matching", "mismatched"}:
                            return SimpleNamespace(returncode=0, stdout=state["luks_uuid"] if condition == "matching" else "other-uuid", stderr="")
                        return SimpleNamespace(returncode=4 if condition == "uuid-error" else 1, stdout="", stderr="I/O error" if condition == "uuid-error" else "")
                    if argv[1] == "isLuks":
                        return SimpleNamespace(returncode=1, stdout="", stderr="I/O error" if condition == "isLuks-error" else "")
                    return SimpleNamespace(returncode=0 if condition == "signature" else 2,
                                           stdout="TYPE=crypto_LUKS" if condition == "signature" else "",
                                           stderr="I/O error" if condition == "blkid-error" else "")

                stack.enter_context(mock.patch.object(backend.subprocess, "run", side_effect=probe))
                readable = stack.enter_context(mock.patch.object(backend, "_assert_partition_readable",
                                             side_effect=OSError("I/O error") if condition == "read-error" else None))
                emit = stack.enter_context(mock.patch.object(backend, "emit"))
                if condition == "absent":
                    backend.discard_prepared("/dev/test")
                    readable.assert_called_once_with("/dev/test3")
                    self.assertFalse(work.exists())
                    emit.assert_called_once()
                    self.assertEqual(emit.call_args.args[0], "discarded")
                else:
                    with self.assertRaisesRegex(backend.InstallError, "Cannot prove"):
                        backend.discard_prepared("/dev/test")
                    self.assertTrue((work / "setup.key").exists())
                    emit.assert_not_called()
                self.assertFalse(any(argv[0] in {"wipefs", "sgdisk"} or argv[1] in {"luksFormat", "luksRemoveKey"} for argv in probes))

    def test_discard_refuses_an_encrypted_attempt(self):
        with self.retained_state("encrypted") as (work, state, stack):
            probes = stack.enter_context(mock.patch.object(backend, "_prepared_volume_status"))
            with self.assertRaisesRegex(backend.InstallError, "Use recover or resume"):
                backend.discard_prepared("/dev/test")
            probes.assert_not_called()
            self.assertTrue((work / "setup.key").exists())

    def test_recovery_confirmation_accepts_only_expected_command(self):
        for line in ("\n", "yes\n", "confirm-recovery-now\n"):
            with self.subTest(line=line), mock.patch.object(backend.sys, "stdin", io.StringIO(line)):
                with self.assertRaisesRegex(backend.InstallError, "temporary key was retained"):
                    backend._wait_for_confirmation()
        with mock.patch.object(backend.sys, "stdin", io.StringIO("confirm-recovery\n")):
            backend._wait_for_confirmation()

    def test_failed_tpm_probe_reports_unavailable(self):
        with mock.patch.object(backend.Path, "exists", return_value=True), mock.patch.object(
            backend, "run", side_effect=backend.InstallError("probe failed")
        ):
            self.assertFalse(backend._tpm_available())

    def test_unlock_verification_does_not_require_an_exclusive_mapping(self):
        # Keep the installation mapping busy to reproduce the live installer
        # failure without touching real disks or requiring a TPM.
        for failure, mode in ((None, "development"), (None, "production"), ("busy-root", "development"), ("tpm", "development"), ("recovery", "development")):
            with self.subTest(failure=failure, mode=mode), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                target = Path(directory) / "target"
                state_root = Path(directory) / "state"
                work = state_root / "disk-123"
                active_mounts = []
                mapping_open = False
                commands = []
                injected = False

                def command(argv, **kwargs):
                    nonlocal mapping_open, injected
                    commands.append(argv)
                    operation = argv[1] if argv[0] in ("cryptsetup", "systemd-cryptsetup") else argv[0]
                    if "--token-only" in argv:
                        operation = "tpm"
                    elif "--test-passphrase" in argv and str(work / "recovery.key") in argv:
                        operation = "recovery"
                    if operation == failure and not injected:
                        injected = True
                        raise backend.InstallError("simulated busy device")
                    if argv[0] == "mount":
                        active_mounts.append(argv[-1])
                    elif argv[0] == "umount":
                        self.assertEqual(argv[-1], active_mounts[-1])
                        if failure == "busy-root" and argv[-1] == str(target):
                            return SimpleNamespace(returncode=32, stderr="target is busy")
                        active_mounts.pop()
                    elif argv[:2] == ["cryptsetup", "open"] and "--test-passphrase" not in argv:
                        mapping_open = True
                    elif argv[:2] == ["cryptsetup", "close"]:
                        self.assertTrue(mapping_open)
                        return SimpleNamespace(returncode=5, stderr="Device is still in use")
                    elif "--token-only" in argv:
                        self.assertEqual(len(active_mounts), 3)
                        self.assertTrue(mapping_open)
                        self.assertIn("--test-passphrase", argv)
                        self.assertEqual(argv[argv.index("--token-type") + 1], "systemd-tpm2")
                        self.assertNotIn("--key-file", argv)
                    elif "--test-passphrase" in argv and "--key-file" in argv:
                        self.assertIn("--disable-external-tokens", argv)
                    return SimpleNamespace(stdout="test-uuid", returncode=2)

                for name, value in {
                    "requirements": {"ok": True},
                    "resolve_selected_disk": {"path": "/dev/test", "stable_path": "/dev/test",
                                              "device_id": 123, "size": 40 * 1024**3, "serial": "test"},
                    "load_config": {"SOURCE_IMAGE": "test", "TARGET_IMAGE": "test", "BUILD_MODE": mode},
                    "_check_scratch": None,
                    "_configure_installed_system": None,
                    "_wait_for_confirmation": None,
                    "_assert_disk_idle": None,
                }.items():
                    stack.enter_context(mock.patch.object(backend, name, return_value=value))
                stack.enter_context(mock.patch.object(backend, "MOUNTPOINT", target))
                stack.enter_context(mock.patch.object(backend, "STATE_ROOT", state_root))
                stack.enter_context(mock.patch.object(backend.uuid, "uuid4", return_value="test-uuid"))
                stack.enter_context(mock.patch.object(backend, "_partition_path", side_effect=lambda disk, n: f"{disk}{n}"))
                stack.enter_context(mock.patch.object(backend, "run", side_effect=command))
                stack.enter_context(mock.patch.object(backend.subprocess, "run", side_effect=command))
                emit = stack.enter_context(mock.patch.object(backend, "emit"))

                if failure in ("tpm", "recovery"):
                    with self.assertRaisesRegex(backend.InstallError, "simulated busy device") as error:
                        backend.install("/dev/test")
                    self.assertIn("Recovery state retained", str(error.exception))
                    self.assertIn("resume --disk /dev/test", str(error.exception))
                    self.assertTrue((work / "setup.key").exists())
                    self.assertTrue((work / "recovery.key").exists())
                    self.assertEqual(backend.json.loads((work / "state.json").read_text())["phase"], "installed")
                    self.assertFalse(any(call.args[0] == "complete" for call in emit.call_args_list))
                    self.assertFalse(any(call.args[0] == "recovery-key" for call in emit.call_args_list))
                else:
                    backend.install("/dev/test")
                    self.assertTrue(any(call.args[0] == "complete" for call in emit.call_args_list))
                    self.assertEqual(sum("--token-only" in argv for argv in commands), 1)
                    # Flush all writes before best-effort unmounting, even if
                    # a live-session process continues to hold the root busy.
                    self.assertLess(commands.index(["sync"]), next(
                        i for i, argv in enumerate(commands) if argv[0] == "umount"
                    ))
                self.assertFalse(any(argv[0] == "systemd-cryptsetup" for argv in commands))
                bootc = next(argv for argv in commands if argv[0] == "bootc")
                self.assertIn("rd.luks.name=test-uuid=" + backend.MAPPER_NAME, bootc)
                if mode == "production":
                    self.assertIn("--enforce-container-sigpolicy", bootc)
                    self.assertIn("--run-fetch-check", bootc)
                    self.assertNotIn("--skip-fetch-check", bootc)
                    self.assertLess(next(i for i, argv in enumerate(commands) if argv[0] == "skopeo"),
                                    next(i for i, argv in enumerate(commands) if argv[0] == "wipefs"))
                else:
                    self.assertIn("--skip-fetch-check", bootc)
                # The only close attempt is best-effort cleanup, after verification.
                self.assertEqual(commands[-1], ["cryptsetup", "close", backend.MAPPER_NAME])
                self.assertEqual(active_mounts, [str(target)] if failure == "busy-root" else [])
                self.assertTrue(mapping_open)

    def test_setup_key_removal_check_rejects_token_unlock_and_other_errors(self):
        for code in (0, 1, 2, 3, 4, 5, -9):
            with self.subTest(code=code), mock.patch.object(
                backend.subprocess, "run",
                return_value=SimpleNamespace(returncode=code, stderr="diagnostic"),
            ) as run:
                if code == 2:
                    backend._verify_setup_key_removed("/dev/test3", Path("/run/setup.key"))
                else:
                    with self.assertRaises(backend.InstallError) as error:
                        backend._verify_setup_key_removed("/dev/test3", Path("/run/setup.key"))
                    if code != 0:
                        self.assertIn("diagnostic", str(error.exception))
                argv = run.call_args.args[0]
                self.assertIn("--disable-external-tokens", argv)
                self.assertEqual(argv[argv.index("--key-file") + 1], "/run/setup.key")
                self.assertNotIn("--token-only", argv)

    def test_finalization_faults_preserve_private_keys_and_can_resume(self):
        for fault in ("confirmation", "remove", "verify", "sync"):
            with self.subTest(fault=fault), self.retained_state() as (work, state, stack):
                stack.enter_context(mock.patch.object(backend, "emit"))
                stack.enter_context(mock.patch.object(backend, "_verify_unlock_methods"))
                accepted = stack.enter_context(mock.patch.object(backend, "_key_accepted", return_value=True))
                verify = stack.enter_context(mock.patch.object(backend, "_verify_setup_key_removed",
                                             side_effect=backend.InstallError("verification fault") if fault == "verify" else None))
                confirm = stack.enter_context(mock.patch.object(backend, "_wait_for_confirmation",
                                              side_effect=backend.InstallError("confirmation fault") if fault == "confirmation" else None))

                def command(argv, **kwargs):
                    if ((fault == "remove" and argv[:2] == ["cryptsetup", "luksRemoveKey"])
                            or (fault == "sync" and argv == ["sync"])):
                        raise backend.InstallError("command fault")
                    return SimpleNamespace(stdout=state["luks_uuid"])

                commands = stack.enter_context(mock.patch.object(backend, "run", side_effect=command))
                with self.assertRaises(backend.InstallError):
                    backend._finish_install(work, state)
                for name in ("setup.key", "recovery.key", "state.json"):
                    self.assertTrue((work / name).exists())
                    self.assertEqual(stat.S_IMODE((work / name).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
                self.assertNotIn((work / "recovery.key").read_text(), (work / "state.json").read_text())
                # Retry after a slot was removed must not attempt removal again.
                accepted.return_value = fault not in {"sync", "verify"}
                verify.side_effect = None
                confirm.side_effect = None
                commands.side_effect = lambda argv, **kwargs: SimpleNamespace(stdout=state["luks_uuid"])
                commands.reset_mock()
                backend.resume("/dev/test")
                self.assertFalse(work.exists())
                if fault in {"sync", "verify"}:
                    self.assertFalse(any(call.args[0][:2] == ["cryptsetup", "luksRemoveKey"] for call in commands.call_args_list))
                self.assertFalse(any(call.args[0][0] in {"wipefs", "sgdisk", "mkfs.ext4", "mkfs.fat", "bootc"} for call in commands.call_args_list))

    def test_resume_rejects_wrong_uuid_or_disk_without_exposing_keys(self):
        for mismatch in ("uuid", "disk", "boot", "permissions", "symlink"):
            with self.subTest(mismatch=mismatch), self.retained_state() as (work, state, stack):
                key = (work / "recovery.key").read_text()
                if mismatch == "disk":
                    state["disk"]["path"] = "/dev/replacement"
                elif mismatch == "boot":
                    state["boot_id"] = "other-boot"
                backend._save_state(work, state)
                if mismatch == "permissions":
                    (work / "setup.key").chmod(0o644)
                elif mismatch == "symlink":
                    (work / "setup.key").unlink()
                    (work / "setup.key").symlink_to(work / "recovery.key")
                commands = stack.enter_context(mock.patch.object(backend, "run",
                                              return_value=SimpleNamespace(stdout="replacement-uuid" if mismatch == "uuid" else state["luks_uuid"])))
                emit = stack.enter_context(mock.patch.object(backend, "emit"))
                with self.assertRaises(backend.InstallError) as error:
                    backend.resume("/dev/test")
                self.assertNotIn(key, str(error.exception))
                emit.assert_not_called()
                self.assertFalse(any(call.args[0][:2] == ["cryptsetup", "luksRemoveKey"] for call in commands.call_args_list))

    def test_recover_early_failure_enrolls_key_then_requires_saved_confirmation(self):
        with self.retained_state("encrypted") as (work, state, stack):
            commands = stack.enter_context(mock.patch.object(backend, "run", return_value=SimpleNamespace(stdout=state["luks_uuid"])))
            stack.enter_context(mock.patch.object(backend, "_key_accepted", side_effect=[False, True, True]))
            stack.enter_context(mock.patch.object(backend, "_verify_setup_key_removed"))
            confirm = stack.enter_context(mock.patch.object(backend, "_wait_for_confirmation"))
            emit = stack.enter_context(mock.patch.object(backend, "emit"))
            backend.resume("/dev/test", recovery_only=True)
            confirm.assert_called_once()
            self.assertTrue(any(call.args[0][:2] == ["cryptsetup", "luksAddKey"] for call in commands.call_args_list))
            self.assertTrue(any(call.args[0] == "recovery-key" for call in emit.call_args_list))
            self.assertFalse(any(call.args[0] == "complete" for call in emit.call_args_list))
            self.assertFalse(work.exists())

    def test_resume_incomplete_deployment_does_not_finalize_it(self):
        with self.retained_state("encrypted") as (work, state, stack):
            stack.enter_context(mock.patch.object(backend, "run", return_value=SimpleNamespace(stdout=state["luks_uuid"])))
            finish = stack.enter_context(mock.patch.object(backend, "_finish_install"))
            with self.assertRaisesRegex(backend.InstallError, "deployment is incomplete"):
                backend.resume("/dev/test")
            finish.assert_not_called()
            self.assertTrue((work / "setup.key").exists())

    def test_parallel_installer_operation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, "STATE_ROOT", Path(directory) / "state"):
            with backend._installer_lock(), self.assertRaisesRegex(backend.InstallError, "still running"):
                with backend._installer_lock():
                    self.fail("lock was acquired twice")

    def test_live_iso_is_permissive_without_disabling_selinux(self):
        config = (ROOT / "installer/iso.yaml").read_text(encoding="utf-8")
        self.assertIn("enforcing=0", config)
        self.assertNotIn("selinux=0", config.lower())

    def test_live_image_disables_gnome_tour(self):
        containerfile = (ROOT / "Containerfile.installer").read_text(encoding="utf-8")
        self.assertIn("/usr/share/applications/org.gnome.Tour.desktop", containerfile)
        self.assertIn("/usr/share/dbus-1/services/org.gnome.Tour.service", containerfile)

    def test_recovery_key_is_high_entropy_grouped_text(self):
        keys = {backend._recovery_key() for _ in range(20)}
        self.assertEqual(len(keys), 20)
        self.assertTrue(all(backend.RECOVERY_PATTERN.fullmatch(key) for key in keys))

    def test_load_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "install.conf"
            config.write_text(
                "SOURCE_IMAGE=localhost/test:base\n"
                "TARGET_IMAGE=ghcr.io/example/test:latest\n",
                encoding="utf-8",
            )
            self.assertEqual(backend.load_config(config), {
                "SOURCE_IMAGE": "localhost/test:base",
                "TARGET_IMAGE": "ghcr.io/example/test:latest",
            })

    def test_load_config_rejects_missing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "install.conf"
            config.write_text("SOURCE_IMAGE=test\n", encoding="utf-8")
            with self.assertRaises(backend.InstallError):
                backend.load_config(config)

    def test_production_config_requires_exact_repository_and_pinned_source(self):
        digest = "a" * 64
        valid_source = backend.IMAGE_REPOSITORY + "@sha256:" + digest
        valid_target = backend.IMAGE_REPOSITORY + ":latest"
        for mode, source, target, valid in (
            ("production", valid_source, valid_target, True),
            ("production", valid_source, valid_source, True),
            ("production", valid_target, valid_target, False),
            ("production", valid_source, "ghcr.io/attacker/mertensiaos:latest", False),
            ("production", valid_source, backend.IMAGE_REPOSITORY + "-other:latest", False),
            ("production", valid_source, "", False),
            ("invalid", valid_source, valid_target, False),
        ):
            with self.subTest(mode=mode, source=source, target=target), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "install.conf"
                path.write_text(f"BUILD_MODE={mode}\nSOURCE_IMAGE={source}\nTARGET_IMAGE={target}\n")
                if valid:
                    self.assertEqual(backend.load_config(path)["SOURCE_IMAGE"], valid_source)
                else:
                    with self.assertRaises(backend.InstallError):
                        backend.load_config(path)

    def test_root_fstab_entry_is_removed_but_boot_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/fstab").write_text(
                "UUID=root / ext4 defaults 0 1\nUUID=boot /boot ext4 defaults 0 2\n",
                encoding="utf-8",
            )
            backend._remove_root_fstab_entry(root)
            result = (root / "etc/fstab").read_text(encoding="utf-8")
            self.assertNotIn("UUID=root", result)
            self.assertIn("UUID=boot", result)

    def test_configuration_updates_deployment_not_physical_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "ostree/deploy/default/deploy/checksum.0"
            (deployment / "etc").mkdir(parents=True)
            fstab = "UUID=root / ext4 defaults 0 1\nUUID=boot /boot ext4 defaults 0 2\n"
            (deployment / "etc/fstab").write_text(fstab)
            (root / "etc").mkdir()
            (root / "etc/fstab").write_text(fstab)
            with mock.patch.object(backend, "run", return_value=SimpleNamespace(stdout=str(deployment) + "\n")) as run:
                backend._configure_installed_system(root, "luks-test-uuid")
            run.assert_called_once_with(
                ["ostree", "admin", "--sysroot=" + str(root), "--print-current-dir"],
                capture=True,
            )
            self.assertEqual((deployment / "etc/fstab").read_text(), "UUID=boot /boot ext4 defaults 0 2\n")
            self.assertEqual(
                (deployment / "etc/crypttab").read_text(),
                "mertensia-root UUID=luks-test-uuid none tpm2-device=auto,x-initrd.attach\n",
            )
            self.assertEqual((root / "etc/fstab").read_text(), fstab)
            self.assertFalse((root / "etc/crypttab").exists())
            self.assertFalse((root / "var").exists())

    def test_configuration_rejects_missing_or_external_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output in ("", "ostree/deploy/default", str(root.parent)):
                with self.subTest(output=output), mock.patch.object(
                    backend, "run", return_value=SimpleNamespace(stdout=output)
                ), self.assertRaises(backend.InstallError):
                    backend._configure_installed_system(root, "luks-test-uuid")
            self.assertFalse((root / "etc").exists())


if __name__ == "__main__":
    unittest.main()
