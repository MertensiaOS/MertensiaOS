"""Image policy, scratch-space checks, and installed deployment configuration."""

from __future__ import annotations

import re
import shutil
import tempfile
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
    value = commands.run(["podman", "image", "inspect", "--format", "{{.Size}}", source], capture=True).stdout.strip()
    try:
        size = int(value)
    except ValueError as error:
        raise commands.InstallError("Could not determine the installation image size.") from error
    if size <= 0:
        raise commands.InstallError("Could not determine the installation image size.")
    return size


def check_target_space(image_size: int, available: int) -> None:
    # Allow room for both the deployment and exported image layers, plus
    # filesystem metadata. They now share the encrypted target volume.
    required = 2 * image_size + 1024**3
    if available < required:
        need = (required + 1024**3 - 1) // 1024**3
        have = available // 1024**3
        raise commands.InstallError(
            f"Installation needs about {need} GiB of free disk space on the target system volume, "
            f"including temporary image data, but only {have} GiB is available. Choose a larger disk."
        )


def install_image(sysroot: Path, image_size: int, argv: list[str]) -> None:
    if not sysroot.is_mount():
        raise commands.InstallError("The encrypted target system volume is not mounted.")
    check_target_space(image_size, shutil.disk_usage(sysroot).free)
    with tempfile.TemporaryDirectory(prefix=".mertensia-install-", dir=sysroot) as scratch:
        # containers/image uses /var/tmp for large exported layer files. A
        # private bind mount redirects those files and TMPDIR to disk without
        # changing the live desktop's mounts. Self-bind scratch so bootc's
        # empty-root check sees an allowed mount point. The namespace disappears
        # before scratch is removed and the encrypted target is unmounted.
        # bootc mirrors /var/tmp from /proc/1/root during setup. Give it a PID
        # namespace and matching /proc so that mirror preserves our disk bind.
        # When its PID 1 exits, remaining storage helpers are killed too.
        commands.run([
            "unshare", "--mount", "--pid", "--fork", "--kill-child=SIGKILL",
            "--mount-proc", "--propagation", "private", "--",
            "sh", "-eu", "-c",
            'mount --bind "$1" "$1"; mount --bind "$1" /var/tmp; '
            'shift; export TMPDIR=/var/tmp; exec "$@"',
            "mertensia-bootc", scratch, *argv,
        ])
