#!/usr/bin/python3
"""Build-only desktop policy, shared by payload and installer after RPM installs."""

from fnmatch import fnmatchcase
from pathlib import Path
import subprocess


# Package families are intentional: remove every foreign-architecture emulator
# and every SSSD provider together. Never ask DNF to autoremove their libraries.
EXCLUDED = (
    "openssh-server", "sssd", "sssd-*", "adcli", "adcli-selinux",
    "qemu-user-static", "qemu-user-static-*", "NetworkManager-cloud-setup",
    "cloud-utils-growpart", "WALinuxAgent-udev", "gnome-tour", "nfs-utils",
    "gnome-software-rpm-ostree", "gnome-software-fedora-langpacks",
)
MASKED = (
    "sshd.service", "sshd.socket", "sshd@.service", "sssd.service",
    "nm-cloud-setup.service", "nm-cloud-setup.timer", "nfs-server.service",
    "dnf5daemon-server.service", "flatpak-add-fedora-repos.service",
    "rpm-ostree-countme.timer", "rpm-ostree-countme.service",
)


def inventory():
    return set(subprocess.check_output(
        ["rpm", "-qa", "--qf", "%{NAME}\\n"], text=True).splitlines())


def excluded(name):
    return any(fnmatchcase(name, pattern) for pattern in EXCLUDED)


def clean():
    before = inventory()
    removing = sorted(name for name in before if excluded(name))
    if removing:
        # Refuse dependency cascades BEFORE changing the image. RPM verifies
        # that this exact set can be erased without breaking installed requires.
        subprocess.run(["rpm", "-e", "--test", *removing], check=True)
        print("Desktop removal set: " + ", ".join(removing), flush=True)
        subprocess.run(["dnf", "-y", "--setopt=clean_requirements_on_remove=False",
                        "remove", *removing], check=True)
        after = inventory()
        if before - after != set(removing) or after - before:
            raise RuntimeError("DNF changed packages outside the reviewed removal set")
    # nfs-utils erasure scriptlets disable this shared target even though its
    # owning client package remains. Preserve client startup and GSS helpers.
    subprocess.run(["systemctl", "enable", "nfs-client.target"], check=True)
    # A mask also blocks manual, dependency and D-Bus systemd activation.
    for unit in MASKED:
        if Path("/usr/lib/systemd/system", unit).exists():
            subprocess.run(["systemctl", "disable", unit], check=True)
    subprocess.run(["systemctl", "mask", *MASKED], check=True)
    # D-Bus can use Exec= without systemd: eliminate that fallback as well.
    Path("/usr/share/dbus-1/system-services/org.rpm.dnf.v0.service").unlink(missing_ok=True)
    # Remove the packaged wheel-user implicit YES. Our image rule denies all
    # DNF daemon actions, including a daemon started directly by an administrator.
    Path("/usr/share/polkit-1/rules.d/org.rpm.dnf.v0.rules").unlink(missing_ok=True)


if __name__ == "__main__":
    clean()
