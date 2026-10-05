"""Run the reseal shell against fake devices and commands, never real disks."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
FAKE_RECOVERY_KEY = "disposable test recovery key"
FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import stat
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
record = {"command": name, "args": args}
key_file = None
if "--key-file" in args:
    key_file = Path(args[args.index("--key-file") + 1])
for arg in args:
    if arg.startswith("--unlock-key-file="):
        key_file = Path(arg.split("=", 1)[1])
if key_file is not None:
    # Verify handling without ever logging plaintext key material.
    record["key_mode"] = stat.S_IMODE(key_file.stat().st_mode)
    record["key_matches"] = key_file.read_text() == os.environ["FAKE_RECOVERY_KEY"]
with open(os.environ["FAKE_RESEAL_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")

if name == "findmnt":
    mountpoint = args[args.index("--mountpoint") + 1]
    value = os.environ["FAKE_SYSROOT_SOURCE" if mountpoint == "/sysroot" else "FAKE_ROOT_SOURCE"]
    if value:
        print(value)
    else:
        sys.exit(1)
elif name == "readlink":
    print(os.environ.get("FAKE_CANONICAL_SOURCE", "/dev/dm-0"))
elif name == "cryptsetup":
    if args[0] == "status":
        if os.environ.get("FAKE_STATUS_FAIL") == "1":
            sys.exit(1)
        print("/dev/mapper/mertensia-root is active.")
        print("  type: " + os.environ.get("FAKE_LUKS_TYPE", "LUKS2"))
        print("  device: " + os.environ.get("FAKE_BACKING_DEVICE", "/dev/fake-root-partition"))
    elif "--token-only" in args:
        if os.environ.get("FAKE_TPM_VERIFY_FAIL") == "1":
            sys.exit(2)
    elif os.environ.get("FAKE_RECOVERY_VERIFY_FAIL") == "1":
        sys.exit(2)
elif name == "bootctl":
    print("Secure Boot: " + os.environ.get("FAKE_SECURE_BOOT", "enabled"))
elif name == "systemd-ask-password":
    print(os.environ["FAKE_RECOVERY_KEY"])
elif name == "systemd-cryptenroll":
    if os.environ.get("FAKE_ENROLL_FAIL") == "1":
        sys.exit(1)
elif name == "shred":
    Path(args[-1]).unlink()
'''


class ResealRootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("bash"):
            raise unittest.SkipTest("reseal shell tests require bash")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.keys = self.work / "keys"
        self.keys.mkdir()
        self.log = self.work / "commands.jsonl"
        self.tpm = self.work / "tpmrm0"
        self.tpm.touch()
        mapping = self.work / "sys/class/block/dm-0/dm/name"
        mapping.parent.mkdir(parents=True)
        mapping.write_text("mertensia-root\n")
        for name in ("findmnt", "readlink", "cryptsetup", "bootctl", "systemd-ask-password",
                     "systemd-cryptenroll", "shred"):
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            tool.chmod(0o755)

        # The same shell logic runs unprivileged. Only the privilege guard and
        # hardware/runtime fixture paths are substituted; every operation that
        # could change an encrypted device is a fake executable above.
        source = (ROOT / "system/bin/mertensia-reseal-root").read_text()
        guard = 'if [[ $EUID -ne 0 ]]; then\n    exec pkexec "$0" "$@"\nfi'
        self.assertIn(guard, source)
        source = source.replace(guard, ": # privilege guard omitted in test fixture")
        source = source.replace("/sys/class/block/", str(self.work / "sys/class/block") + "/")
        source = source.replace("/dev/tpmrm0", str(self.tpm))
        source = source.replace("/dev/tpm0", str(self.work / "absent-tpm0"))
        source = source.replace("--tmpdir=/run", "--tmpdir=" + str(self.keys))
        self.script = self.work / "reseal-root"
        self.script.write_text(source)
        self.environment = dict(os.environ) | {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_RESEAL_LOG": str(self.log),
            "FAKE_RECOVERY_KEY": FAKE_RECOVERY_KEY,
            "FAKE_SYSROOT_SOURCE": "/dev/mapper/mertensia-root",
            "FAKE_ROOT_SOURCE": "overlay",
        }

    def run_reseal(self, **environment):
        result = subprocess.run(["bash", str(self.script)], env=self.environment | environment,
                                capture_output=True, text=True, timeout=15)
        self.assertNotIn(FAKE_RECOVERY_KEY, result.stdout + result.stderr)
        self.assertEqual(list(self.keys.iterdir()), [])
        if self.log.exists():
            self.assertNotIn(FAKE_RECOVERY_KEY, self.log.read_text())
        return result

    def calls(self, command=None):
        records = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [record for record in records if command is None or record["command"] == command]

    def assert_no_enrollment(self):
        self.assertEqual(self.calls("systemd-cryptenroll"), [])

    def test_composefs_uses_exact_sysroot_mount_without_inspecting_overlay_root(self):
        result = self.run_reseal()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("now matches", result.stdout)
        self.assertEqual(self.calls("findmnt"), [{"command": "findmnt", "args": [
            "--mountpoint", "/sysroot", "--nofsroot", "-nro", "SOURCE"]}])
        self.assertEqual(self.calls("cryptsetup")[0]["args"], ["status", "mertensia-root"])

    def test_non_composefs_root_falls_back_to_exact_root_mount(self):
        result = self.run_reseal(FAKE_SYSROOT_SOURCE="", FAKE_ROOT_SOURCE="/dev/mapper/mertensia-root")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["args"][1] for call in self.calls("findmnt")], ["/sysroot", "/"])
        self.assertTrue(all("--nofsroot" in call["args"] for call in self.calls("findmnt")))

    def test_canonical_dm_source_is_supported(self):
        result = self.run_reseal(FAKE_SYSROOT_SOURCE="/dev/dm-0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("readlink")[0]["args"], ["-f", "/dev/dm-0"])

    def test_unencrypted_root_is_rejected_before_password_or_enrollment(self):
        result = self.run_reseal(FAKE_SYSROOT_SOURCE="/dev/sda3", FAKE_ROOT_SOURCE="overlay")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("systemd-ask-password"), [])
        self.assert_no_enrollment()

    def test_invalid_or_inactive_mapping_is_rejected_before_enrollment(self):
        for fault in ({"FAKE_CANONICAL_SOURCE": "/dev/sda3"}, {"FAKE_STATUS_FAIL": "1"},
                      {"FAKE_LUKS_TYPE": "PLAIN"}, {"FAKE_BACKING_DEVICE": ""}):
            with self.subTest(fault=fault):
                self.log.unlink(missing_ok=True)
                result = self.run_reseal(**fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls("systemd-ask-password"), [])
                self.assert_no_enrollment()

    def test_secure_boot_and_tpm_are_required_before_password_prompt(self):
        result = self.run_reseal(FAKE_SECURE_BOOT="disabled")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("systemd-ask-password"), [])
        self.assert_no_enrollment()
        self.log.unlink()
        self.tpm.unlink()
        result = self.run_reseal()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("systemd-ask-password"), [])
        self.assert_no_enrollment()

    def test_recovery_verification_precedes_enrollment_and_tpm_only_verification(self):
        result = self.run_reseal()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.calls()
        recovery = next(call for call in self.calls("cryptsetup") if "--key-file" in call["args"])
        enrollment = self.calls("systemd-cryptenroll")[0]
        tpm = next(call for call in self.calls("cryptsetup") if "--token-only" in call["args"])
        self.assertIn("--test-passphrase", recovery["args"])
        self.assertIn("--disable-external-tokens", recovery["args"])
        self.assertEqual(recovery["key_mode"], 0o600)
        self.assertTrue(recovery["key_matches"])
        self.assertEqual(enrollment["key_mode"], 0o600)
        self.assertTrue(enrollment["key_matches"])
        self.assertIn("--wipe-slot=tpm2", enrollment["args"])
        self.assertIn("--tpm2-pcrs=7", enrollment["args"])
        self.assertEqual(tpm["args"], ["open", "--test-passphrase", "--token-only",
                                       "--token-type", "systemd-tpm2", "/dev/fake-root-partition"])
        self.assertLess(records.index(recovery), records.index(enrollment))
        self.assertLess(records.index(enrollment), records.index(tpm))

    def test_wrong_recovery_key_does_not_change_tpm_enrollment(self):
        result = self.run_reseal(FAKE_RECOVERY_VERIFY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_enrollment()
        self.assertNotIn("now matches", result.stdout)

    def test_enrollment_or_tpm_verification_failure_never_claims_success(self):
        for fault in ({"FAKE_ENROLL_FAIL": "1"}, {"FAKE_TPM_VERIFY_FAIL": "1"}):
            with self.subTest(fault=fault):
                self.log.unlink(missing_ok=True)
                result = self.run_reseal(**fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("now matches", result.stdout)
                self.assertEqual(len(self.calls("systemd-cryptenroll")), 1)
                if "FAKE_TPM_VERIFY_FAIL" in fault:
                    self.assertIn("could not be verified", result.stderr)


if __name__ == "__main__":
    unittest.main()
