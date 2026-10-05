"""Private recovery state and encryption-key lifecycle for interrupted installs.

Setup keys remain in this live session until recovery confirmation and verified
keyslot removal. Inspection failures never prove that retained keys are safe to
discard.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import commands, constants, devices


def recovery_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(6)) for _ in range(8))


def wait_for_confirmation() -> None:
    line = sys.stdin.readline()
    if line.strip() != "confirm-recovery":
        raise commands.InstallError("Recovery key confirmation was not received; the temporary key was retained.")


def require_private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise commands.InstallError("Installer recovery state has unsafe ownership or permissions.")


def state_root() -> None:
    constants.STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    require_private(constants.STATE_ROOT, directory=True)


@contextmanager
def installer_lock():
    state_root()
    fd = os.open(constants.STATE_ROOT / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require_private(constants.STATE_ROOT / "lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise commands.InstallError("Another installer operation is still running.") from error
        yield
    finally:
        os.close(fd)


def write_private(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_state(work: Path, state: dict[str, Any]) -> None:
    write_private(work / "state.json", json.dumps(state).encode("utf-8"))


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def new_state(disk: dict[str, Any], root_part: str) -> tuple[Path, dict[str, Any]]:
    state_root()
    work = constants.STATE_ROOT / f"disk-{disk['device_id']}"
    if work.exists() or work.is_symlink():
        raise commands.InstallError("Recovery state already exists for this disk. Use resume or recover before a new installation.")
    work.mkdir(mode=0o700)
    state = {"version": 1, "boot_id": boot_id(), "disk": disk,
             "root_part": root_part, "luks_uuid": str(uuid.uuid4()), "phase": "prepared"}
    try:
        write_private(work / "setup.key", secrets.token_bytes(64))
        recovery = recovery_key()
        if not constants.RECOVERY_PATTERN.fullmatch(recovery):
            raise commands.InstallError("Internal recovery key generation failure.")
        write_private(work / "recovery.key", recovery.encode("ascii"))
        save_state(work, state)
    except BaseException:
        # No LUKS header uses these keys yet, so a failed initialization is disposable.
        shutil.rmtree(work)
        raise
    return work, state


def read_state(requested_disk: str, *, verify_volume: bool = True) -> tuple[Path, dict[str, Any]]:
    state_root()
    info = os.stat(requested_disk)
    if not stat.S_ISBLK(info.st_mode):
        raise commands.InstallError("The selected path is not a block device.")
    work = constants.STATE_ROOT / f"disk-{info.st_rdev}"
    require_private(work, directory=True)
    require_private(work / "state.json")
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("version") != 1
            or state.get("boot_id") != boot_id()
            or state.get("phase") not in {"prepared", "encrypted", "installed", "confirmed"}
            or not isinstance(state.get("disk"), dict)):
        raise commands.InstallError("Installer recovery state is invalid or belongs to another boot.")
    disk = state["disk"]
    required = {"path", "stable_path", "size", "serial", "device_id"}
    if (not required.issubset(disk) or disk["device_id"] != info.st_rdev
            or os.path.realpath(requested_disk) != os.path.realpath(disk["path"])
            or not isinstance(state.get("root_part"), str)
            or not isinstance(state.get("luks_uuid"), str)):
        raise commands.InstallError("The requested disk does not match the retained recovery state.")
    devices.assert_disk_identity(disk)
    root_part = devices.partition_path(disk["path"], 3)
    if os.path.realpath(root_part) != os.path.realpath(state["root_part"]):
        raise commands.InstallError("The encrypted partition does not match the retained recovery state.")
    if verify_volume:
        actual_uuid = commands.run(["cryptsetup", "luksUUID", root_part], capture=True).stdout.strip()
        if not actual_uuid or actual_uuid != state["luks_uuid"]:
            raise commands.InstallError("The LUKS UUID does not match the retained recovery state.")
    for name in ("setup.key", "recovery.key"):
        require_private(work / name)
    recovery = (work / "recovery.key").read_text(encoding="ascii")
    if not constants.RECOVERY_PATTERN.fullmatch(recovery):
        raise commands.InstallError("The retained recovery key is invalid.")
    return work, state


def assert_partition_readable(root_part: str) -> None:
    fd = os.open(root_part, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode) or len(os.pread(fd, 4096, 0)) != 4096:
            raise commands.InstallError("The encrypted partition could not be read safely.")
    finally:
        os.close(fd)


def prepared_volume_status(state: dict[str, Any]) -> str:
    """Return absent only after independent, error-free probes and readable I/O."""
    root_part = state["root_part"]
    try:
        devices.assert_disk_identity(state["disk"])
        if devices.partition_path(state["disk"]["path"], 3) != root_part:
            return "unknown"
        result = subprocess.run(["cryptsetup", "luksUUID", root_part], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and result.stdout.strip():
            return "matching" if result.stdout.strip() == state["luks_uuid"] else "mismatched"
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            return "unknown"
        # isLuks exit 1 alone is insufficient: it can also represent an error.
        result = subprocess.run(["cryptsetup", "isLuks", root_part], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            return "unknown"
        result = subprocess.run(["blkid", "--probe", "--output", "export", root_part], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 2 or result.stdout.strip() or result.stderr.strip():
            return "unknown"
        # Missing devices and read failures must never release retained keys.
        assert_partition_readable(root_part)
        return "absent"
    except (commands.InstallError, OSError):
        return "unknown"


def discard_prepared(requested_disk: str) -> None:
    work, state = read_state(requested_disk, verify_volume=False)
    if state["phase"] != "prepared":
        raise commands.InstallError("This attempt created an encrypted volume. Use recover or resume; do not discard its keys.")
    devices.assert_workspace_idle()
    if prepared_volume_status(state) != "absent":
        raise commands.InstallError("Cannot prove the absence of an encrypted volume. Recovery state retained; do not restart before recovering it.")
    shutil.rmtree(work)
    commands.emit("discarded", message="No encrypted volume was created. Unused recovery state was released; you may retry installation or restart the live system.")


def key_accepted(root_part: str, key_file: Path) -> bool:
    result = subprocess.run(
        ["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens",
         "--key-file", str(key_file), root_part],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode not in {0, 2}:
        raise commands.InstallError(f"Could not verify a retained encryption key (exit {result.returncode}).")
    return result.returncode == 0


def verify_unlock_methods(root_part: str, recovery_file: Path) -> None:
    commands.run(["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens", "--key-file", str(recovery_file), root_part])
    commands.run(["cryptsetup", "open", "--test-passphrase", "--token-only", "--token-type", "systemd-tpm2", root_part])


def finish_install(work: Path, state: dict[str, Any]) -> None:
    root_part = state["root_part"]
    recovery_file = work / "recovery.key"
    commands.emit("phase", id="verify", message="Verifying unlock methods", progress=0.86)
    verify_unlock_methods(root_part, recovery_file)
    commands.emit("recovery-key", key=recovery_file.read_text(encoding="ascii"),
         message="Save this root recovery key before continuing.")
    wait_for_confirmation()
    state["phase"] = "confirmed"
    save_state(work, state)
    commands.emit("phase", id="finalize", message="Removing the temporary setup key", progress=0.94)
    remove_setup_key(work, root_part)
    commands.emit("complete", message="MertensiaOS is installed. Restart and remove the installation media.")


def remove_setup_key(work: Path, root_part: str) -> None:
    setup_key = work / "setup.key"
    # A previous finalization can have removed the slot before being interrupted.
    if key_accepted(root_part, setup_key):
        commands.run(["cryptsetup", "luksRemoveKey", root_part, str(setup_key)])
    verify_setup_key_removed(root_part, setup_key)
    commands.run(["sync"])
    shutil.rmtree(work)


def recovery_notice(state: dict[str, Any]) -> str:
    command = "resume" if state["phase"] in {"installed", "confirmed"} else "recover"
    absent = state["phase"] == "prepared" and prepared_volume_status(state) == "absent"
    if absent:
        command = "discard-prepared"
    # Paths are argv in the helper. Quote the human-readable command safely too.
    introduction = (
        "No encrypted volume was created. Unused recovery state can be released before retrying; restarting the live system is also safe for this failed attempt. "
        if absent else "Recovery state retained only in this live session. Do not restart before recovering it. "
    )
    return (introduction +
            "Run: " + shlex.join(["pkexec", "/usr/libexec/mertensia-installer-helper", command,
                                  "--disk", state["disk"]["stable_path"]]))


def resume(requested_disk: str, *, recovery_only: bool = False) -> None:
    work, state = read_state(requested_disk)
    try:
        if recovery_only:
            # Earlier failures may occur between format and recovery-key enrollment.
            if not key_accepted(state["root_part"], work / "recovery.key"):
                if not key_accepted(state["root_part"], work / "setup.key"):
                    raise commands.InstallError("Neither retained key unlocks the selected volume.")
                commands.run(["cryptsetup", "luksAddKey", "--key-file", str(work / "setup.key"),
                     "--new-keyfile", str(work / "recovery.key"), state["root_part"]])
            commands.run(["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens",
                 "--key-file", str(work / "recovery.key"), state["root_part"]])
            commands.emit("recovery-key", key=(work / "recovery.key").read_text(encoding="ascii"),
                 message="Save this key, then enter confirm-recovery. The incomplete installation must be installed again.")
            wait_for_confirmation()
            remove_setup_key(work, state["root_part"])
            commands.emit("recovered", message="Recovery key confirmed. A new installation can now erase the incomplete installation.")
            return
        if state["phase"] not in {"installed", "confirmed"}:
            raise commands.InstallError("The system deployment is incomplete. Use recover to save the volume key, then install again.")
        finish_install(work, state)
    except (commands.InstallError, OSError) as error:
        raise commands.InstallError(f"{error}\n{recovery_notice(state)}") from error


def verify_setup_key_removed(root_part: str, setup_key: Path) -> None:
    # Otherwise the enrolled TPM token can unlock the volume even though this
    # particular key file no longer matches any keyslot.
    result = subprocess.run(
        ["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens",
         "--key-file", str(setup_key), root_part],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        raise commands.InstallError("The temporary encryption key could not be removed.")
    # Exit 2 means the key was rejected. Other failures (I/O, arguments, etc.)
    # do not establish that removal succeeded.
    if result.returncode != 2:
        detail = result.stderr.strip()
        raise commands.InstallError(
            f"Could not verify removal of the temporary encryption key (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
