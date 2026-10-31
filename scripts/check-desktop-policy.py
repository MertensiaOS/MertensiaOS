#!/usr/bin/python3
"""Read-only image checks; run inside either built payload or live container."""

from pathlib import Path
import re
import subprocess


packages = set(subprocess.check_output(["rpm", "-qa", "--qf", "%{NAME}\\n"], text=True).splitlines())
for name in packages:
    if re.fullmatch(r"openssh-server|sssd(?:-.*)?|adcli(?:-selinux)?|qemu-user-static(?:-.*)?|"
                    r"NetworkManager-cloud-setup|cloud-utils-growpart|WALinuxAgent-udev|gnome-tour|nfs-utils|"
                    r"gnome-software-(?:rpm-ostree|fedora-langpacks)", name):
        raise SystemExit(f"Excluded desktop package: {name}")
required = {"bootc", "bootupd", "podman", "skopeo", "openssh-clients", "gnome-software",
            "dnf5daemon-server", "dnf5daemon-server-polkit", "NetworkManager", "NetworkManager-wifi",
            "google-noto-fonts-all", "glibc-all-langpacks", "fwupd", "gnome-firmware", "gdm",
            "chromium", "systemd-pam", "systemd-udev", "cryptsetup", "tpm2-tools", "greenboot",
            "nfs-client-utils", "nfsv3-client-utils", "nfsv4-client-utils", "rpcbind", "avahi"}
if required - packages:
    raise SystemExit(f"Missing retained packages: {sorted(required - packages)}")
masked = {"sshd.service", "sshd.socket", "sshd@.service", "sssd.service", "nfs-server.service",
          "nm-cloud-setup.service", "nm-cloud-setup.timer", "dnf5daemon-server.service",
          "flatpak-add-fedora-repos.service", "rpm-ostree-countme.timer", "rpm-ostree-countme.service"}
units = Path("/etc/systemd/system")
for name in masked:
    path = units / name
    if not path.is_symlink() or str(path.readlink()) != "/dev/null":
        raise SystemExit(f"Missing service mask: {name}")
for target in ("multi-user.target", "remote-fs.target"):
    link = units / f"{target}.wants/nfs-client.target"
    if not link.is_symlink() or str(link.readlink()) != "/usr/lib/systemd/system/nfs-client.target":
        raise SystemExit(f"Missing NFS client activation link: {link}")
for link in units.rglob("*"):
    if link.is_symlink() and link.parent.suffix in (".wants", ".requires"):
        if link.name in masked | {"rpcbind.socket", "rpcbind.service"}:
            raise SystemExit(f"Unwanted boot activation link: {link}")
for path in Path("/usr/share/dbus-1/system-services").glob("*.service"):
    text = path.read_text()
    if "org.rpm.dnf.v0" in text or "dnf5daemon-server" in text:
        raise SystemExit(f"Unwanted DNF D-Bus activation: {path}")
assert not Path("/usr/share/polkit-1/rules.d/org.rpm.dnf.v0.rules").exists()
assert Path("/usr/share/polkit-1/rules.d/00-mertensia-no-host-packages.rules").is_file()
assert not Path("/usr/share/applications/org.gnome.Tour.desktop").exists()
assert not Path("/usr/share/dbus-1/services/org.gnome.Tour.service").exists()
assert Path("/usr/lib/systemd/systemd-cryptsetup").is_file()
assert Path("/usr/lib64/cryptsetup/libcryptsetup-token-systemd-tpm2.so").is_file()
subprocess.run(["authselect", "check"], check=True)
print(f"Desktop policy verified: {len(packages)} packages; masks, boot links, D-Bus, Polkit and authselect checked.")
