"""Hardware discovery and fail-closed disk and workspace safety checks."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import commands, constants


def secure_boot_enabled() -> bool:
    efivars = Path("/sys/firmware/efi/efivars")
    if not efivars.is_dir():
        return False
    for value in efivars.glob("SecureBoot-*"):
        data = value.read_bytes()
        return len(data) >= 5 and data[4] == 1
    return False


def tpm_available() -> bool:
    if not (Path("/dev/tpmrm0").exists() or Path("/dev/tpm0").exists()):
        return False
    try:
        commands.run(["systemd-cryptenroll", "--tpm2-device=list"])
        return True
    except (OSError, commands.InstallError):
        return False


def requirements() -> dict[str, Any]:
    arch = os.uname().machine
    uefi = Path("/sys/firmware/efi").is_dir()
    secure_boot = secure_boot_enabled()
    tpm = tpm_available()
    return {
        "architecture": arch,
        "architecture_ok": arch == "x86_64",
        "uefi": uefi,
        "secure_boot": secure_boot,
        "tpm2": tpm,
        "ok": arch == "x86_64" and uefi and secure_boot and tpm,
    }


def live_backing_devices() -> set[str]:
    excluded: set[str] = set()
    for target in ("/", "/run/initramfs/live", "/run/initramfs/squashed"):
        found = subprocess.run(
            ["findmnt", "-nro", "SOURCE", target], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ).stdout.strip()
        if not found.startswith("/dev/"):
            continue
        try:
            tree = json.loads(commands.run(["lsblk", "-J", "-o", "PATH,PKNAME", found], capture=True).stdout)
            for item in tree.get("blockdevices", []):
                excluded.add(item.get("path", ""))
                if item.get("pkname"):
                    excluded.add("/dev/" + item["pkname"])
        except (commands.InstallError, subprocess.CalledProcessError, json.JSONDecodeError):
            excluded.add(found)
    return excluded


def flatten_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for device in devices:
        output.append(device)
        output.extend(flatten_devices(device.get("children", [])))
    return output


def block_devices(disk: str | None = None) -> list[dict[str, Any]]:
    argv = ["lsblk", "--bytes", "--json", "--output", constants.BLOCK_FIELDS]
    if disk:
        argv.append(disk)
    devices = json.loads(commands.run(argv, capture=True).stdout).get("blockdevices")
    if not isinstance(devices, list):
        raise commands.InstallError("Could not inspect block devices safely.")
    return devices


def swap_devices() -> set[str]:
    # Read the kernel's active swap table independently of lsblk's mount data.
    lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("Filename"):
        raise commands.InstallError("Could not inspect active swap safely.")
    return {os.path.realpath(line.split()[0]) for line in lines[1:] if line.split()}


def device_holders(device: dict[str, Any]) -> list[str]:
    name = device.get("kname") or Path(device.get("path", "")).name
    if not name or "/" in name or name in {".", ".."}:
        raise commands.InstallError("Could not identify the disk's kernel device.")
    # Missing/inaccessible sysfs is an inspection failure, never proof of safety.
    return [holder.name for holder in (constants.SYS_BLOCK / name / "holders").iterdir()]


def disk_busy_reason(device: dict[str, Any], swaps: set[str]) -> str | None:
    for node in flatten_devices([device]):
        if any(node.get("mountpoints") or []):
            return "a filesystem or swap partition is mounted"
        if os.path.realpath(node.get("path", "")) in swaps:
            return "swap is active"
        if node.get("type") not in {"disk", "part"} or device_holders(node):
            return "a device mapping holds the disk open"
    return None


def list_disks() -> list[dict[str, Any]]:
    devices = block_devices()
    excluded = live_backing_devices()
    swaps = swap_devices()
    disks: list[dict[str, Any]] = []
    by_id = Path("/dev/disk/by-id")
    for item in devices:
        path = item.get("path", "")
        if item.get("type") != "disk" or item.get("ro") or path in excluded:
            continue
        if int(item.get("size") or 0) < constants.MINIMUM_DISK_BYTES:
            continue
        if any(p in excluded for p in (child.get("path", "") for child in flatten_devices(item.get("children", [])))):
            continue
        if disk_busy_reason(item, swaps):
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
            if disk["size"] < constants.MINIMUM_DISK_BYTES:
                raise commands.InstallError("The selected disk must be at least 24 GiB.")
            info = os.stat(disk["path"])
            if not stat.S_ISBLK(info.st_mode):
                raise commands.InstallError("The selected path is not a block device.")
            disk["device_id"] = info.st_rdev
            return disk
    raise commands.InstallError("The selected disk is unavailable or contains the running live system.")


def assert_disk_identity(disk: dict[str, Any]) -> dict[str, Any]:
    info = os.stat(disk["path"])
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != disk["device_id"]:
        raise commands.InstallError("The selected disk's device identity changed.")
    if os.path.realpath(disk["stable_path"]) != os.path.realpath(disk["path"]):
        raise commands.InstallError("The selected disk's stable identifier changed.")
    devices = block_devices(disk["path"])
    candidates = [item for item in devices if item.get("type") == "disk"
                  and os.path.realpath(item.get("path", "")) == os.path.realpath(disk["path"])]
    if len(candidates) != 1:
        raise commands.InstallError("Could not identify the selected whole disk safely.")
    item = candidates[0]
    if (item.get("ro") or int(item.get("size") or 0) != disk["size"]
            or (item.get("serial") or "").strip() != disk["serial"]):
        raise commands.InstallError("The selected disk's properties changed.")
    return item


def assert_disk_idle(disk: dict[str, Any]) -> None:
    item = assert_disk_identity(disk)
    excluded = live_backing_devices()
    if any(node.get("path") in excluded for node in flatten_devices([item])):
        raise commands.InstallError("The selected disk contains the running live system.")
    reason = disk_busy_reason(item, swap_devices())
    if reason:
        raise commands.InstallError(f"The selected disk is in use: {reason}. Unmount it before installing.")


def mount_targets() -> list[Path]:
    lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    if not lines or any(len(line.split()) < 6 for line in lines):
        raise commands.InstallError("Could not inspect the installer's mount namespace safely.")
    return [Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
            for line in lines]


def assert_workspace_idle() -> None:
    if os.path.lexists(Path("/dev/mapper") / constants.MAPPER_NAME):
        raise commands.InstallError("The installer encryption mapping is already active. Recover the previous attempt before installing another disk.")
    if constants.MOUNTPOINT.is_symlink():
        raise commands.InstallError("The installer target is a symbolic link; refusing to modify any disk.")
    target = constants.MOUNTPOINT.resolve()
    if any(mount == target or mount.is_relative_to(target) for mount in mount_targets()):
        raise commands.InstallError("The installer target is still mounted. Recover the previous attempt before installing another disk.")


def partition_path(disk: str, number: int) -> str:
    result = commands.run(["lsblk", "-nrpo", "PATH,PARTN", disk], capture=True).stdout
    for line in result.splitlines():
        columns = line.split()
        if len(columns) == 2 and columns[1] == str(number):
            return columns[0]
    raise commands.InstallError(f"Partition {number} did not appear on {disk}.")
