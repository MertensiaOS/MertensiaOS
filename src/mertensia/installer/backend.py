"""Privileged live-installer orchestration and command-line entry point.

The UI consumes one JSON object per line. Recovery confirmation is handled by
recovery.py, which retains temporary keys until unlock methods are verified.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import commands, constants, deployment, devices, recovery


def install(requested_disk: str) -> None:
    check = devices.requirements()
    if not check["ok"]:
        raise commands.InstallError("Installation requires x86_64 UEFI, enabled Secure Boot, and a usable TPM 2.0 device.")
    disk = devices.resolve_selected_disk(requested_disk)
    config = deployment.load_config()
    deployment.check_scratch(config["SOURCE_IMAGE"])
    if config.get("BUILD_MODE", "development") == "production":
        # Fail before disk modification if the signed update target is unreachable.
        commands.run(["skopeo", "inspect", "docker://" + config["TARGET_IMAGE"]])
    recovery.state_root()
    if (constants.STATE_ROOT / f"disk-{disk['device_id']}").exists():
        raise commands.InstallError("Recovery state already exists for this disk. Use resume or recover before installing again.")

    work: Path | None = None
    state: dict[str, Any] | None = None
    mapper = Path("/dev/mapper") / constants.MAPPER_NAME
    mounts: list[Path] = []
    mapper_open = False
    root_part = ""
    try:
        commands.emit("phase", id="partition", message="Preparing the selected disk", progress=0.05)
        # Recheck immediately before the first destructive command. Do not force
        # wipefs past its own protection against mounted filesystems.
        devices.assert_workspace_idle()
        devices.assert_disk_idle(disk)
        commands.run(["wipefs", "--all", disk["path"]])
        commands.run(["sgdisk", "--zap-all", disk["path"]])
        commands.run(["sgdisk", "--new=1:1MiB:+1GiB", "--typecode=1:ef00", "--change-name=1:Mertensia EFI", disk["path"]])
        commands.run(["sgdisk", "--new=2:0:+2GiB", "--typecode=2:ea00", "--change-name=2:Mertensia boot", disk["path"]])
        commands.run(["sgdisk", "--new=3:0:0", "--typecode=3:8309", "--change-name=3:Mertensia root", disk["path"]])
        commands.run(["partprobe", disk["path"]])
        commands.run(["udevadm", "settle"])
        efi_part = devices.partition_path(disk["path"], 1)
        boot_part = devices.partition_path(disk["path"], 2)
        root_part = devices.partition_path(disk["path"], 3)

        commands.emit("phase", id="encrypt", message="Encrypting the system volume", progress=0.15)
        commands.run(["mkfs.fat", "-F", "32", "-n", "MERT-EFI", efi_part])
        commands.run(["mkfs.ext4", "-F", "-L", "mertensia-boot", boot_part])
        work, state = recovery.new_state(disk, root_part)
        setup_key = work / "setup.key"
        recovery_file = work / "recovery.key"
        commands.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--uuid", state["luks_uuid"],
             "--key-file", str(setup_key), root_part])
        state["phase"] = "encrypted"
        recovery.save_state(work, state)
        commands.run(["cryptsetup", "open", "--key-file", str(setup_key), root_part, constants.MAPPER_NAME])
        mapper_open = True
        commands.run(["mkfs.ext4", "-F", "-L", "mertensia-root", str(mapper)])

        commands.run(["cryptsetup", "luksAddKey", "--key-file", str(setup_key), "--new-keyfile", str(recovery_file), root_part])
        commands.run(
            [
                "systemd-cryptenroll",
                "--unlock-key-file=" + str(setup_key),
                "--tpm2-device=auto",
                "--tpm2-pcrs=7",
                root_part,
            ]
        )

        commands.emit("phase", id="install", message="Installing MertensiaOS", progress=0.30)
        constants.MOUNTPOINT.mkdir(parents=True, exist_ok=True)
        commands.run(["mount", str(mapper), str(constants.MOUNTPOINT)])
        mounts.append(constants.MOUNTPOINT)
        (constants.MOUNTPOINT / "boot/efi").mkdir(parents=True, exist_ok=True)
        commands.run(["mount", boot_part, str(constants.MOUNTPOINT / "boot")])
        mounts.append(constants.MOUNTPOINT / "boot")
        (constants.MOUNTPOINT / "boot/efi").mkdir(parents=True, exist_ok=True)
        commands.run(["mount", efi_part, str(constants.MOUNTPOINT / "boot/efi")])
        mounts.append(constants.MOUNTPOINT / "boot/efi")
        root_uuid = commands.run(["blkid", "-s", "UUID", "-o", "value", str(mapper)], capture=True).stdout.strip()
        boot_uuid = commands.run(["blkid", "-s", "UUID", "-o", "value", boot_part], capture=True).stdout.strip()
        luks_uuid = commands.run(["cryptsetup", "luksUUID", root_part], capture=True).stdout.strip()
        if luks_uuid != state["luks_uuid"]:
            raise commands.InstallError("The formatted LUKS UUID does not match the retained recovery state.")
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
            "--karg", "rd.luks.name=" + luks_uuid + "=" + constants.MAPPER_NAME,
            "--karg", "rd.luks.options=" + luks_uuid + "=tpm2-device=auto",
            str(constants.MOUNTPOINT),
        ]
        if config.get("BUILD_MODE", "development") == "production":
            bootc_args[3:3] = ["--enforce-container-sigpolicy", "--run-fetch-check"]
        else:
            bootc_args.insert(3, "--skip-fetch-check")
        commands.run(bootc_args)
        deployment.configure_installed_system(constants.MOUNTPOINT, luks_uuid)
        state["phase"] = "installed"
        recovery.save_state(work, state)
        recovery.finish_install(work, state)
    except (commands.InstallError, OSError) as error:
        if work and state and work.exists():
            raise commands.InstallError(f"{error}\n{recovery.recovery_notice(state)}") from error
        raise
    finally:
        for target in reversed(mounts):
            subprocess.run(["umount", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if mapper_open:
            subprocess.run(["cryptsetup", "close", constants.MAPPER_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        commands.emit("error", message="The installer helper must run as root.")
        return 1
    try:
        if args.command == "probe":
            commands.emit("requirements", **devices.requirements())
        elif args.command == "list-disks":
            commands.emit("disks", disks=devices.list_disks())
        else:
            with recovery.installer_lock():
                if args.command == "install":
                    install(args.disk)
                elif args.command == "discard-prepared":
                    recovery.discard_prepared(args.disk)
                else:
                    recovery.resume(args.disk, recovery_only=args.command == "recover")
        return 0
    except (commands.InstallError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        message = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            command = Path(str(error.cmd[0])).name if error.cmd else "command"
            message = f"{command} failed while installing. The selected disk may be partially modified."
        commands.emit("error", message=message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
