"""Disk capacity, private scratch lifetime, and deployment command failures."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from mertensia.installer import backend, commands, deployment, devices


GIB = 1024**3


class InstallerDeploymentTests(unittest.TestCase):
    def test_image_size_must_be_known_and_positive(self):
        for value in ("", "unknown", "0", "-1"):
            with self.subTest(value=value), mock.patch.object(
                commands, "run", return_value=SimpleNamespace(stdout=value)
            ), self.assertRaisesRegex(commands.InstallError, "image size"):
                deployment.source_image_size("source")
        with mock.patch.object(commands, "run", return_value=SimpleNamespace(stdout=str(5 * GIB))):
            self.assertEqual(deployment.source_image_size("source"), 5 * GIB)

    def test_capacity_includes_deployment_and_scratch_on_the_same_disk(self):
        deployment.check_target_space(5 * GIB, 11 * GIB)
        with self.assertRaisesRegex(commands.InstallError, "free disk space.*Choose a larger disk") as error:
            deployment.check_target_space(5 * GIB, 11 * GIB - 1)
        self.assertNotIn("RAM", str(error.exception))

    def test_missing_image_or_insufficient_capacity_never_erases_a_disk(self):
        for image_size in ("invalid", str(12 * GIB)):
            with self.subTest(image_size=image_size), mock.patch.object(
                devices, "requirements", return_value={"ok": True}
            ), mock.patch.object(devices, "resolve_selected_disk", return_value={"size": 24 * GIB}), mock.patch.object(
                deployment, "load_config", return_value={"SOURCE_IMAGE": "source", "TARGET_IMAGE": "target"}
            ), mock.patch.object(commands, "run", return_value=SimpleNamespace(stdout=image_size)) as run:
                with self.assertRaises(commands.InstallError):
                    backend.install("/dev/test")
                self.assertEqual([call.args[0][0] for call in run.call_args_list], ["podman"])

    def test_unmounted_or_full_target_never_starts_image_installation(self):
        for mounted, free in ((False, 40 * GIB), (True, 0)):
            with self.subTest(mounted=mounted, free=free), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                Path, "is_mount", return_value=mounted
            ), mock.patch.object(deployment.shutil, "disk_usage", return_value=SimpleNamespace(free=free)), mock.patch.object(
                commands, "run"
            ) as run:
                target = Path(directory)
                with self.assertRaises(commands.InstallError):
                    deployment.install_image(target, 5 * GIB, ["bootc"])
                run.assert_not_called()
                self.assertEqual(list(target.iterdir()), [])

    def test_scratch_is_private_on_target_and_removed_on_success_or_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                Path, "is_mount", return_value=True
            ), mock.patch.object(deployment.shutil, "disk_usage", return_value=SimpleNamespace(free=40 * GIB)) as usage:
                target = Path(directory)
                sentinel = target / "keep"
                sentinel.write_text("unrelated data")

                def install(argv):
                    scratch = Path(argv[argv.index("mertensia-bootc") + 1])
                    self.assertEqual(scratch.parent, target)
                    self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
                    (scratch / "partial-blob").write_text("exported image data")
                    if fail:
                        raise commands.InstallError("bootc failed")

                with mock.patch.object(commands, "run", side_effect=install):
                    if fail:
                        with self.assertRaisesRegex(commands.InstallError, "bootc failed"):
                            deployment.install_image(target, 5 * GIB, ["bootc"])
                    else:
                        deployment.install_image(target, 5 * GIB, ["bootc"])
                usage.assert_called_once_with(target)
                self.assertEqual(list(target.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_text(), "unrelated data")

    def test_shell_binds_before_exec_and_aborts_when_either_mount_fails(self):
        # Execute the real shell wrapper with fake mount/bootc binaries, so a
        # mount failure must prevent bootc and arguments must survive quoting.
        for failed_mount in (0, 1, 2):
            with self.subTest(failed_mount=failed_mount), tempfile.TemporaryDirectory(prefix="installer test ") as directory:
                root = Path(directory)
                target, bin_dir = root / "target", root / "bin"
                target.mkdir()
                bin_dir.mkdir()
                mount_log, bootc_log = root / "mounts.jsonl", root / "bootc.json"
                mount = bin_dir / "mount"
                mount.write_text(
                    "#!/usr/bin/python3\nimport json, os, pathlib, sys\n"
                    "p = pathlib.Path(os.environ['MOUNT_LOG'])\n"
                    "with p.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "sys.exit(32 if len(p.read_text().splitlines()) == int(os.environ['FAILED_MOUNT']) else 0)\n"
                )
                bootc = bin_dir / "bootc"
                bootc.write_text(
                    "#!/usr/bin/python3\nimport json, os, pathlib, sys\n"
                    "pathlib.Path(os.environ['BOOTC_LOG']).write_text(json.dumps([sys.argv[1:], os.environ['TMPDIR']]))\n"
                )
                mount.chmod(0o755)
                bootc.chmod(0o755)
                args = ["bootc", "install", "to-filesystem", "--source-imgref", "source; $(false)", str(target)]

                def execute(argv):
                    result = subprocess.run(argv[argv.index("--") + 1:], text=True, capture_output=True, env=dict(
                        os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                        MOUNT_LOG=str(mount_log), BOOTC_LOG=str(bootc_log), FAILED_MOUNT=str(failed_mount),
                    ))
                    if result.returncode:
                        raise commands.InstallError(result.stderr or "mount failed")

                with mock.patch.object(Path, "is_mount", return_value=True), mock.patch.object(
                    deployment.shutil, "disk_usage", return_value=SimpleNamespace(free=40 * GIB)
                ), mock.patch.object(commands, "run", side_effect=execute):
                    if failed_mount:
                        with self.assertRaises(commands.InstallError):
                            deployment.install_image(target, 5 * GIB, args)
                        self.assertFalse(bootc_log.exists())
                    else:
                        deployment.install_image(target, 5 * GIB, args)
                        self.assertEqual(json.loads(bootc_log.read_text()), [args[1:], "/var/tmp"])
                mounts = [json.loads(line) for line in mount_log.read_text().splitlines()]
                self.assertEqual(mounts[0], ["--bind", mounts[0][1], mounts[0][1]])
                self.assertEqual(Path(mounts[0][1]).parent, target)
                if failed_mount != 1:
                    self.assertEqual(mounts[1], ["--bind", mounts[0][1], "/var/tmp"])
                self.assertEqual(list(target.iterdir()), [])
