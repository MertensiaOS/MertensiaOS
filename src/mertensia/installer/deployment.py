"""Image policy, scratch-space checks, and installed deployment configuration."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import commands, constants


def load_config(path: Path = constants.CONFIG) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    required = {"SOURCE_IMAGE", "TARGET_IMAGE"}
    missing = required.difference(result)
    if missing:
        raise commands.InstallError(f"Installer configuration is missing: {', '.join(sorted(missing))}")
    mode = result.get("BUILD_MODE", "development")
    if mode not in {"development", "production"}:
        raise commands.InstallError("Installer BUILD_MODE must be development or production.")
    if mode == "production":
        repository = re.escape(constants.IMAGE_REPOSITORY)
        if not re.fullmatch(repository + r"(?:[:][\w][\w.-]{0,127}|@sha256:[a-f0-9]{64})", result["TARGET_IMAGE"]):
            raise commands.InstallError("Production TARGET_IMAGE must reference the MertensiaOS image repository.")
        if not re.fullmatch(repository + r"@sha256:[a-f0-9]{64}", result["SOURCE_IMAGE"]):
            raise commands.InstallError("Production SOURCE_IMAGE must be a pinned MertensiaOS image digest.")
    return result


def remove_root_fstab_entry(root: Path) -> None:
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


def configure_installed_system(sysroot: Path, luks_uuid: str) -> None:
    # bootc installs an OSTree deployment beneath the physical root. Its /etc
    # is the configuration used at boot; the physical root's /etc is not.
    deployment_name = commands.run(
        ["ostree", "admin", "--sysroot=" + str(sysroot), "--print-current-dir"],
        capture=True,
    ).stdout.strip()
    deployment = Path(deployment_name)
    if not deployment_name or not deployment.is_absolute():
        raise commands.InstallError("Could not locate the installed deployment.")
    deployment = deployment.resolve(strict=True)
    if not deployment.is_relative_to(sysroot.resolve() / "ostree/deploy"):
        raise commands.InstallError("The installed deployment is outside the target sysroot.")
    if not (deployment / "etc").is_dir():
        raise commands.InstallError("The installed deployment has no configuration directory.")
    remove_root_fstab_entry(deployment)
    (deployment / "etc/crypttab").write_text(
        f"{constants.MAPPER_NAME} UUID={luks_uuid} none tpm2-device=auto,x-initrd.attach\n",
        encoding="utf-8",
    )


def source_image_size(source: str) -> int:
    try:
        value = commands.run(["podman", "image", "inspect", "--format", "{{.Size}}", source], capture=True).stdout.strip()
        return int(value)
    except (ValueError, OSError, subprocess.CalledProcessError):
        return 0


def check_scratch(source: str) -> None:
    required = source_image_size(source) + 1024**3
    available = shutil.disk_usage("/var/tmp").free
    if required > 1024**3 and available < required:
        need = (required + 1024**3 - 1) // 1024**3
        have = available // 1024**3
        raise commands.InstallError(
            f"The live installer needs about {need} GiB of temporary memory but only {have} GiB is available. "
            "Close applications or boot this installer on a machine with more RAM."
        )
