#!/usr/bin/python3
"""Privileged backend for the MertensiaOS live installer.

The UI consumes one JSON object per line.  The only command accepted on stdin
during an install is ``confirm-recovery``; until then the temporary LUKS setup
key is deliberately retained so an interrupted install remains recoverable.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


CONFIG = Path("/usr/lib/mertensia-installer/install.conf")
STATE_ROOT = Path("/run/mertensia-install-state")
MOUNTPOINT = STATE_ROOT / "target"
MAPPER_NAME = "mertensia-root"
MINIMUM_DISK_BYTES = 24 * 1024**3
RECOVERY_PATTERN = re.compile(r"^[A-Z2-9]{6}(?:-[A-Z2-9]{6}){7}$")
SYS_BLOCK = Path("/sys/class/block")
IMAGE_REPOSITORY = "ghcr.io/mertensiaos/mertensiaos"
BLOCK_FIELDS = "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS"


class InstallError(RuntimeError):
    pass


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, separators=(",", ":")), flush=True)


def run(
    argv: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        input=input_text,
        text=True,
        check=False,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        detail = result.stderr.strip()
        command = Path(argv[0]).name
        raise InstallError(
            f"{command} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )

    return result


def load_config(path: Path = CONFIG) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    required = {"SOURCE_IMAGE", "TARGET_IMAGE"}
    missing = required.difference(result)
    if missing:
        raise InstallError(f"Installer configuration is missing: {', '.join(sorted(missing))}")
    mode = result.get("BUILD_MODE", "development")
    if mode not in {"development", "production"}:
        raise InstallError("Installer BUILD_MODE must be development or production.")
    if mode == "production":
        repository = re.escape(IMAGE_REPOSITORY)
        if not re.fullmatch(repository + r"(?:[:][\w][\w.-]{0,127}|@sha256:[a-f0-9]{64})", result["TARGET_IMAGE"]):
            raise InstallError("Production TARGET_IMAGE must reference the MertensiaOS image repository.")
        if not re.fullmatch(repository + r"@sha256:[a-f0-9]{64}", result["SOURCE_IMAGE"]):
            raise InstallError("Production SOURCE_IMAGE must be a pinned MertensiaOS image digest.")
    return result


def _secure_boot_enabled() -> bool:
    efivars = Path("/sys/firmware/efi/efivars")
    if not efivars.is_dir():
        return False
    for value in efivars.glob("SecureBoot-*"):
        data = value.read_bytes()
        return len(data) >= 5 and data[4] == 1
    return False


def _tpm_available() -> bool:
    if not (Path("/dev/tpmrm0").exists() or Path("/dev/tpm0").exists()):
        return False
    try:
        run(["systemd-cryptenroll", "--tpm2-device=list"])
        return True
    except (OSError, InstallError):
        return False


def requirements() -> dict[str, Any]:
    arch = os.uname().machine
    uefi = Path("/sys/firmware/efi").is_dir()
    secure_boot = _secure_boot_enabled()
    tpm = _tpm_available()
    return {
        "architecture": arch,
        "architecture_ok": arch == "x86_64",
        "uefi": uefi,
        "secure_boot": secure_boot,
        "tpm2": tpm,
        "ok": arch == "x86_64" and uefi and secure_boot and tpm,
    }


def _live_backing_devices() -> set[str]:
    excluded: set[str] = set()
    for target in ("/", "/run/initramfs/live", "/run/initramfs/squashed"):
        found = subprocess.run(
            ["findmnt", "-nro", "SOURCE", target], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ).stdout.strip()
        if not found.startswith("/dev/"):
            continue
        try:
            tree = json.loads(run(["lsblk", "-J", "-o", "PATH,PKNAME", found], capture=True).stdout)
            for item in tree.get("blockdevices", []):
                excluded.add(item.get("path", ""))
                if item.get("pkname"):
                    excluded.add("/dev/" + item["pkname"])
        except (InstallError, subprocess.CalledProcessError, json.JSONDecodeError):
            excluded.add(found)
    return excluded


def _flatten_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for device in devices:
        output.append(device)
        output.extend(_flatten_devices(device.get("children", [])))
    return output


def _block_devices(disk: str | None = None) -> list[dict[str, Any]]:
    argv = ["lsblk", "--bytes", "--json", "--output", BLOCK_FIELDS]
    if disk:
        argv.append(disk)
    devices = json.loads(run(argv, capture=True).stdout).get("blockdevices")
    if not isinstance(devices, list):
        raise InstallError("Could not inspect block devices safely.")
    return devices


def _swap_devices() -> set[str]:
    # Read the kernel's active swap table independently of lsblk's mount data.
    lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("Filename"):
        raise InstallError("Could not inspect active swap safely.")
    return {os.path.realpath(line.split()[0]) for line in lines[1:] if line.split()}


def _device_holders(device: dict[str, Any]) -> list[str]:
    name = device.get("kname") or Path(device.get("path", "")).name
    if not name or "/" in name or name in {".", ".."}:
        raise InstallError("Could not identify the disk's kernel device.")
    # Missing/inaccessible sysfs is an inspection failure, never proof of safety.
    return [holder.name for holder in (SYS_BLOCK / name / "holders").iterdir()]


def _disk_busy_reason(device: dict[str, Any], swaps: set[str]) -> str | None:
    for node in _flatten_devices([device]):
        if any(node.get("mountpoints") or []):
            return "a filesystem or swap partition is mounted"
        if os.path.realpath(node.get("path", "")) in swaps:
            return "swap is active"
        if node.get("type") not in {"disk", "part"} or _device_holders(node):
            return "a device mapping holds the disk open"
    return None


def list_disks() -> list[dict[str, Any]]:
    devices = _block_devices()
    excluded = _live_backing_devices()
    swaps = _swap_devices()
    disks: list[dict[str, Any]] = []
    by_id = Path("/dev/disk/by-id")
    for item in devices:
        path = item.get("path", "")
        if item.get("type") != "disk" or item.get("ro") or path in excluded:
            continue
        if int(item.get("size") or 0) < MINIMUM_DISK_BYTES:
            continue
        if any(p in excluded for p in (child.get("path", "") for child in _flatten_devices(item.get("children", [])))):
            continue
        if _disk_busy_reason(item, swaps):
            continue
        stable = ""
        if by_id.is_dir():
            candidates = []
            for link in by_id.iterdir():
                if "-part" in link.name:
                    continue
                try:
                    if link.resolve() == Path(path):
                        candidates.append(str(link))
                except OSError:
                    pass
            if candidates:
                stable = sorted(candidates, key=lambda p: ("wwn-" not in p, len(p), p))[0]
        disks.append(
            {
                "path": path,
                "stable_path": stable or path,
                "size": int(item.get("size") or 0),
                "model": (item.get("model") or "Unknown disk").strip(),
                "serial": (item.get("serial") or "").strip(),
                "transport": item.get("tran") or "",
                "removable": bool(item.get("rm")),
            }
        )
    return disks


def resolve_selected_disk(requested: str) -> dict[str, Any]:
    requested_real = os.path.realpath(requested)
    for disk in list_disks():
        if requested in (disk["path"], disk["stable_path"]) or requested_real == os.path.realpath(disk["path"]):
            if disk["size"] < MINIMUM_DISK_BYTES:
                raise InstallError("The selected disk must be at least 24 GiB.")
            info = os.stat(disk["path"])
            if not stat.S_ISBLK(info.st_mode):
                raise InstallError("The selected path is not a block device.")
            disk["device_id"] = info.st_rdev
            return disk
    raise InstallError("The selected disk is unavailable or contains the running live system.")


def _assert_disk_identity(disk: dict[str, Any]) -> dict[str, Any]:
    info = os.stat(disk["path"])
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != disk["device_id"]:
        raise InstallError("The selected disk's device identity changed.")
    if os.path.realpath(disk["stable_path"]) != os.path.realpath(disk["path"]):
        raise InstallError("The selected disk's stable identifier changed.")
    devices = _block_devices(disk["path"])
    candidates = [item for item in devices if item.get("type") == "disk"
                  and os.path.realpath(item.get("path", "")) == os.path.realpath(disk["path"])]
    if len(candidates) != 1:
        raise InstallError("Could not identify the selected whole disk safely.")
    item = candidates[0]
    if (item.get("ro") or int(item.get("size") or 0) != disk["size"]
            or (item.get("serial") or "").strip() != disk["serial"]):
        raise InstallError("The selected disk's properties changed.")
    return item


def _assert_disk_idle(disk: dict[str, Any]) -> None:
    item = _assert_disk_identity(disk)
    excluded = _live_backing_devices()
    if any(node.get("path") in excluded for node in _flatten_devices([item])):
        raise InstallError("The selected disk contains the running live system.")
    reason = _disk_busy_reason(item, _swap_devices())
    if reason:
        raise InstallError(f"The selected disk is in use: {reason}. Unmount it before installing.")


def _mount_targets() -> list[Path]:
    lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    if not lines or any(len(line.split()) < 6 for line in lines):
        raise InstallError("Could not inspect the installer's mount namespace safely.")
    return [Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
            for line in lines]


def _assert_workspace_idle() -> None:
    if os.path.lexists(Path("/dev/mapper") / MAPPER_NAME):
        raise InstallError("The installer encryption mapping is already active. Recover the previous attempt before installing another disk.")
    if MOUNTPOINT.is_symlink():
        raise InstallError("The installer target is a symbolic link; refusing to modify any disk.")
    target = MOUNTPOINT.resolve()
    if any(mount == target or mount.is_relative_to(target) for mount in _mount_targets()):
        raise InstallError("The installer target is still mounted. Recover the previous attempt before installing another disk.")


def _partition_path(disk: str, number: int) -> str:
    result = run(["lsblk", "-nrpo", "PATH,PARTN", disk], capture=True).stdout
    for line in result.splitlines():
        columns = line.split()
        if len(columns) == 2 and columns[1] == str(number):
            return columns[0]
    raise InstallError(f"Partition {number} did not appear on {disk}.")


def _recovery_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(6)) for _ in range(8))


def _wait_for_confirmation() -> None:
    line = sys.stdin.readline()
    if line.strip() != "confirm-recovery":
        raise InstallError("Recovery key confirmation was not received; the temporary key was retained.")


def _require_private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise InstallError("Installer recovery state has unsafe ownership or permissions.")


def _state_root() -> None:
    STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    _require_private(STATE_ROOT, directory=True)


@contextmanager
def _installer_lock():
    _state_root()
    fd = os.open(STATE_ROOT / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _require_private(STATE_ROOT / "lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError("Another installer operation is still running.") from error
        yield
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _save_state(work: Path, state: dict[str, Any]) -> None:
    _write_private(work / "state.json", json.dumps(state).encode("utf-8"))


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _new_state(disk: dict[str, Any], root_part: str) -> tuple[Path, dict[str, Any]]:
    _state_root()
    work = STATE_ROOT / f"disk-{disk['device_id']}"
    if work.exists() or work.is_symlink():
        raise InstallError("Recovery state already exists for this disk. Use resume or recover before a new installation.")
    work.mkdir(mode=0o700)
    state = {"version": 1, "boot_id": _boot_id(), "disk": disk,
             "root_part": root_part, "luks_uuid": str(uuid.uuid4()), "phase": "prepared"}
    try:
        _write_private(work / "setup.key", secrets.token_bytes(64))
        recovery = _recovery_key()
        if not RECOVERY_PATTERN.fullmatch(recovery):
            raise InstallError("Internal recovery key generation failure.")
        _write_private(work / "recovery.key", recovery.encode("ascii"))
        _save_state(work, state)
    except BaseException:
        # No LUKS header uses these keys yet, so a failed initialization is disposable.
        shutil.rmtree(work)
        raise
    return work, state


def _read_state(requested_disk: str, *, verify_volume: bool = True) -> tuple[Path, dict[str, Any]]:
    _state_root()
    info = os.stat(requested_disk)
    if not stat.S_ISBLK(info.st_mode):
        raise InstallError("The selected path is not a block device.")
    work = STATE_ROOT / f"disk-{info.st_rdev}"
    _require_private(work, directory=True)
    _require_private(work / "state.json")
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("version") != 1
            or state.get("boot_id") != _boot_id()
            or state.get("phase") not in {"prepared", "encrypted", "installed", "confirmed"}
            or not isinstance(state.get("disk"), dict)):
        raise InstallError("Installer recovery state is invalid or belongs to another boot.")
    disk = state["disk"]
    required = {"path", "stable_path", "size", "serial", "device_id"}
    if (not required.issubset(disk) or disk["device_id"] != info.st_rdev
            or os.path.realpath(requested_disk) != os.path.realpath(disk["path"])
            or not isinstance(state.get("root_part"), str)
            or not isinstance(state.get("luks_uuid"), str)):
        raise InstallError("The requested disk does not match the retained recovery state.")
    _assert_disk_identity(disk)
    root_part = _partition_path(disk["path"], 3)
    if os.path.realpath(root_part) != os.path.realpath(state["root_part"]):
        raise InstallError("The encrypted partition does not match the retained recovery state.")
    if verify_volume:
        actual_uuid = run(["cryptsetup", "luksUUID", root_part], capture=True).stdout.strip()
        if not actual_uuid or actual_uuid != state["luks_uuid"]:
            raise InstallError("The LUKS UUID does not match the retained recovery state.")
    for name in ("setup.key", "recovery.key"):
        _require_private(work / name)
    recovery = (work / "recovery.key").read_text(encoding="ascii")
    if not RECOVERY_PATTERN.fullmatch(recovery):
        raise InstallError("The retained recovery key is invalid.")
    return work, state


def _assert_partition_readable(root_part: str) -> None:
    fd = os.open(root_part, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode) or len(os.pread(fd, 4096, 0)) != 4096:
            raise InstallError("The encrypted partition could not be read safely.")
    finally:
        os.close(fd)


def _prepared_volume_status(state: dict[str, Any]) -> str:
    """Return absent only after independent, error-free probes and readable I/O."""
    root_part = state["root_part"]
    try:
        _assert_disk_identity(state["disk"])
        if _partition_path(state["disk"]["path"], 3) != root_part:
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
        _assert_partition_readable(root_part)
        return "absent"
    except (InstallError, OSError):
        return "unknown"


def discard_prepared(requested_disk: str) -> None:
    work, state = _read_state(requested_disk, verify_volume=False)
    if state["phase"] != "prepared":
        raise InstallError("This attempt created an encrypted volume. Use recover or resume; do not discard its keys.")
    _assert_workspace_idle()
    if _prepared_volume_status(state) != "absent":
        raise InstallError("Cannot prove the absence of an encrypted volume. Recovery state retained; do not restart before recovering it.")
    shutil.rmtree(work)
    emit("discarded", message="No encrypted volume was created. Unused recovery state was released; you may retry installation or restart the live system.")


def _key_accepted(root_part: str, key_file: Path) -> bool:
    result = subprocess.run(
        ["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens",
         "--key-file", str(key_file), root_part],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode not in {0, 2}:
        raise InstallError(f"Could not verify a retained encryption key (exit {result.returncode}).")
    return result.returncode == 0


def _verify_unlock_methods(root_part: str, recovery_file: Path) -> None:
    run(["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens", "--key-file", str(recovery_file), root_part])
    run(["cryptsetup", "open", "--test-passphrase", "--token-only", "--token-type", "systemd-tpm2", root_part])


def _finish_install(work: Path, state: dict[str, Any]) -> None:
    root_part = state["root_part"]
    recovery_file = work / "recovery.key"
    emit("phase", id="verify", message="Verifying unlock methods", progress=0.86)
    _verify_unlock_methods(root_part, recovery_file)
    emit("recovery-key", key=recovery_file.read_text(encoding="ascii"),
         message="Save this root recovery key before continuing.")
    _wait_for_confirmation()
    state["phase"] = "confirmed"
    _save_state(work, state)
    emit("phase", id="finalize", message="Removing the temporary setup key", progress=0.94)
    _remove_setup_key(work, root_part)
    emit("complete", message="MertensiaOS is installed. Restart and remove the installation media.")


def _remove_setup_key(work: Path, root_part: str) -> None:
    setup_key = work / "setup.key"
    # A previous finalization can have removed the slot before being interrupted.
    if _key_accepted(root_part, setup_key):
        run(["cryptsetup", "luksRemoveKey", root_part, str(setup_key)])
    _verify_setup_key_removed(root_part, setup_key)
    run(["sync"])
    shutil.rmtree(work)


def _recovery_notice(state: dict[str, Any]) -> str:
    command = "resume" if state["phase"] in {"installed", "confirmed"} else "recover"
    absent = state["phase"] == "prepared" and _prepared_volume_status(state) == "absent"
    if absent:
        command = "discard-prepared"
    # Paths are argv in the helper. Quote the human-readable command safely too.
    import shlex
    introduction = (
        "No encrypted volume was created. Unused recovery state can be released before retrying; restarting the live system is also safe for this failed attempt. "
        if absent else "Recovery state retained only in this live session. Do not restart before recovering it. "
    )
    return (introduction +
            "Run: " + shlex.join(["pkexec", "/usr/libexec/mertensia-installer-helper", command,
                                  "--disk", state["disk"]["stable_path"]]))


def resume(requested_disk: str, *, recovery_only: bool = False) -> None:
    work, state = _read_state(requested_disk)
    try:
        if recovery_only:
            # Earlier failures may occur between format and recovery-key enrollment.
            if not _key_accepted(state["root_part"], work / "recovery.key"):
                if not _key_accepted(state["root_part"], work / "setup.key"):
                    raise InstallError("Neither retained key unlocks the selected volume.")
                run(["cryptsetup", "luksAddKey", "--key-file", str(work / "setup.key"),
                     "--new-keyfile", str(work / "recovery.key"), state["root_part"]])
            run(["cryptsetup", "open", "--test-passphrase", "--disable-external-tokens",
                 "--key-file", str(work / "recovery.key"), state["root_part"]])
            emit("recovery-key", key=(work / "recovery.key").read_text(encoding="ascii"),
                 message="Save this key, then enter confirm-recovery. The incomplete installation must be installed again.")
            _wait_for_confirmation()
            _remove_setup_key(work, state["root_part"])
            emit("recovered", message="Recovery key confirmed. A new installation can now erase the incomplete installation.")
            return
        if state["phase"] not in {"installed", "confirmed"}:
            raise InstallError("The system deployment is incomplete. Use recover to save the volume key, then install again.")
        _finish_install(work, state)
    except (InstallError, OSError) as error:
        raise InstallError(f"{error}\n{_recovery_notice(state)}") from error


def _remove_root_fstab_entry(root: Path) -> None:
    fstab = root / "etc/fstab"
    if not fstab.exists():
        return
    kept = []
    for line in fstab.read_text(encoding="utf-8").splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1] == "/":
            continue
        kept.append(line)
    fstab.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _configure_installed_system(sysroot: Path, luks_uuid: str) -> None:
    # bootc installs an OSTree deployment beneath the physical root. Its /etc
    # is the configuration used at boot; the physical root's /etc is not.
    deployment_name = run(
        ["ostree", "admin", "--sysroot=" + str(sysroot), "--print-current-dir"],
        capture=True,
    ).stdout.strip()
    deployment = Path(deployment_name)
    if not deployment_name or not deployment.is_absolute():
        raise InstallError("Could not locate the installed deployment.")
    deployment = deployment.resolve(strict=True)
    if not deployment.is_relative_to(sysroot.resolve() / "ostree/deploy"):
        raise InstallError("The installed deployment is outside the target sysroot.")
    if not (deployment / "etc").is_dir():
        raise InstallError("The installed deployment has no configuration directory.")
    _remove_root_fstab_entry(deployment)
    (deployment / "etc/crypttab").write_text(
        f"{MAPPER_NAME} UUID={luks_uuid} none tpm2-device=auto,x-initrd.attach\n",
        encoding="utf-8",
    )


def _source_image_size(source: str) -> int:
    try:
        value = run(["podman", "image", "inspect", "--format", "{{.Size}}", source], capture=True).stdout.strip()
        return int(value)
    except (ValueError, OSError, subprocess.CalledProcessError):
        return 0


def _check_scratch(source: str) -> None:
    required = _source_image_size(source) + 1024**3
    available = shutil.disk_usage("/var/tmp").free
    if required > 1024**3 and available < required:
        need = (required + 1024**3 - 1) // 1024**3
        have = available // 1024**3
        raise InstallError(
            f"The live installer needs about {need} GiB of temporary memory but only {have} GiB is available. "
            "Close applications or boot this installer on a machine with more RAM."
        )


def _verify_setup_key_removed(root_part: str, setup_key: Path) -> None:
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
        raise InstallError("The temporary encryption key could not be removed.")
    # Exit 2 means the key was rejected. Other failures (I/O, arguments, etc.)
    # do not establish that removal succeeded.
    if result.returncode != 2:
        detail = result.stderr.strip()
        raise InstallError(
            f"Could not verify removal of the temporary encryption key (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )


def install(requested_disk: str) -> None:
    check = requirements()
    if not check["ok"]:
        raise InstallError("Installation requires x86_64 UEFI, enabled Secure Boot, and a usable TPM 2.0 device.")
    disk = resolve_selected_disk(requested_disk)
    config = load_config()
    _check_scratch(config["SOURCE_IMAGE"])
    if config.get("BUILD_MODE", "development") == "production":
        # Fail before disk modification if the signed update target is unreachable.
        run(["skopeo", "inspect", "docker://" + config["TARGET_IMAGE"]])
    _state_root()
    if (STATE_ROOT / f"disk-{disk['device_id']}").exists():
        raise InstallError("Recovery state already exists for this disk. Use resume or recover before installing again.")

    work: Path | None = None
    state: dict[str, Any] | None = None
    mapper = Path("/dev/mapper") / MAPPER_NAME
    mounts: list[Path] = []
    mapper_open = False
    root_part = ""
    try:
        emit("phase", id="partition", message="Preparing the selected disk", progress=0.05)
        # Recheck immediately before the first destructive command. Do not force
        # wipefs past its own protection against mounted filesystems.
        _assert_workspace_idle()
        _assert_disk_idle(disk)
        run(["wipefs", "--all", disk["path"]])
        run(["sgdisk", "--zap-all", disk["path"]])
        run(["sgdisk", "--new=1:1MiB:+1GiB", "--typecode=1:ef00", "--change-name=1:Mertensia EFI", disk["path"]])
        run(["sgdisk", "--new=2:0:+2GiB", "--typecode=2:ea00", "--change-name=2:Mertensia boot", disk["path"]])
        run(["sgdisk", "--new=3:0:0", "--typecode=3:8309", "--change-name=3:Mertensia root", disk["path"]])
        run(["partprobe", disk["path"]])
        run(["udevadm", "settle"])
        efi_part = _partition_path(disk["path"], 1)
        boot_part = _partition_path(disk["path"], 2)
        root_part = _partition_path(disk["path"], 3)

        emit("phase", id="encrypt", message="Encrypting the system volume", progress=0.15)
        run(["mkfs.fat", "-F", "32", "-n", "MERT-EFI", efi_part])
        run(["mkfs.ext4", "-F", "-L", "mertensia-boot", boot_part])
        work, state = _new_state(disk, root_part)
        setup_key = work / "setup.key"
        recovery_file = work / "recovery.key"
        run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--uuid", state["luks_uuid"],
             "--key-file", str(setup_key), root_part])
        state["phase"] = "encrypted"
        _save_state(work, state)
        run(["cryptsetup", "open", "--key-file", str(setup_key), root_part, MAPPER_NAME])
        mapper_open = True
        run(["mkfs.ext4", "-F", "-L", "mertensia-root", str(mapper)])

        run(["cryptsetup", "luksAddKey", "--key-file", str(setup_key), "--new-keyfile", str(recovery_file), root_part])
        run(
            [
                "systemd-cryptenroll",
                "--unlock-key-file=" + str(setup_key),
                "--tpm2-device=auto",
                "--tpm2-pcrs=7",
                root_part,
            ]
        )

        emit("phase", id="install", message="Installing MertensiaOS", progress=0.30)
        MOUNTPOINT.mkdir(parents=True, exist_ok=True)
        run(["mount", str(mapper), str(MOUNTPOINT)])
        mounts.append(MOUNTPOINT)
        (MOUNTPOINT / "boot/efi").mkdir(parents=True, exist_ok=True)
        run(["mount", boot_part, str(MOUNTPOINT / "boot")])
        mounts.append(MOUNTPOINT / "boot")
        (MOUNTPOINT / "boot/efi").mkdir(parents=True, exist_ok=True)
        run(["mount", efi_part, str(MOUNTPOINT / "boot/efi")])
        mounts.append(MOUNTPOINT / "boot/efi")
        root_uuid = run(["blkid", "-s", "UUID", "-o", "value", str(mapper)], capture=True).stdout.strip()
        boot_uuid = run(["blkid", "-s", "UUID", "-o", "value", boot_part], capture=True).stdout.strip()
        luks_uuid = run(["cryptsetup", "luksUUID", root_part], capture=True).stdout.strip()
        if luks_uuid != state["luks_uuid"]:
            raise InstallError("The formatted LUKS UUID does not match the retained recovery state.")
        source = "containers-storage:" + config["SOURCE_IMAGE"]
        bootc_args = [
            "bootc", "install", "to-filesystem",
            "--source-imgref", source,
            "--target-imgref", config["TARGET_IMAGE"],
            "--root-mount-spec", "UUID=" + root_uuid,
            "--boot-mount-spec", "UUID=" + boot_uuid,
            "--skip-finalize",
            "--karg", "rd.luks.uuid=luks-" + luks_uuid,
            # Match the installed crypttab name so switch-root does not try
            # to activate the same partition under a second mapping name.
            "--karg", "rd.luks.name=" + luks_uuid + "=" + MAPPER_NAME,
            "--karg", "rd.luks.options=" + luks_uuid + "=tpm2-device=auto",
            str(MOUNTPOINT),
        ]
        if config.get("BUILD_MODE", "development") == "production":
            bootc_args[3:3] = ["--enforce-container-sigpolicy", "--run-fetch-check"]
        else:
            bootc_args.insert(3, "--skip-fetch-check")
        run(bootc_args)
        _configure_installed_system(MOUNTPOINT, luks_uuid)
        state["phase"] = "installed"
        _save_state(work, state)
        _finish_install(work, state)
    except (InstallError, OSError) as error:
        if work and state and work.exists():
            raise InstallError(f"{error}\n{_recovery_notice(state)}") from error
        raise
    finally:
        for target in reversed(mounts):
            subprocess.run(["umount", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if mapper_open:
            subprocess.run(["cryptsetup", "close", MAPPER_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe")
    sub.add_parser("list-disks")
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--disk", required=True)
    for command in ("resume", "recover", "discard-prepared"):
        recovery_parser = sub.add_parser(command)
        recovery_parser.add_argument("--disk", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        emit("error", message="The installer helper must run as root.")
        return 1
    try:
        if args.command == "probe":
            emit("requirements", **requirements())
        elif args.command == "list-disks":
            emit("disks", disks=list_disks())
        else:
            with _installer_lock():
                if args.command == "install":
                    install(args.disk)
                elif args.command == "discard-prepared":
                    discard_prepared(args.disk)
                else:
                    resume(args.disk, recovery_only=args.command == "recover")
        return 0
    except (InstallError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        message = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            command = Path(str(error.cmd[0])).name if error.cmd else "command"
            message = f"{command} failed while installing. The selected disk may be partially modified."
        emit("error", message=message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
