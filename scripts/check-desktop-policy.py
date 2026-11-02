#!/usr/bin/python3
"""Read-only image checks; run inside either built payload or live container."""

from pathlib import Path
import configparser
import re
import subprocess


def check_group_merging(nsswitch: Path) -> None:
    # polkit expands wheel through getgrgid(), so files must merge its group
    # members with systemd's homed memberships instead of stopping at files.
    # Fedora bootc also stores image groups in /usr/lib/group via altfiles;
    # that source must merge successfully too, before consulting systemd.
    if not re.search(r"^[ \t]*group:[ \t]+files[ \t]+\[SUCCESS=merge\][ \t]+"
                     r"(?:altfiles[ \t]+\[SUCCESS=merge\][ \t]+)?"
                     r"systemd(?:[ \t]+\[SUCCESS=merge\])?[ \t]*(?:#.*)?$",
                     nsswitch.read_text(), re.MULTILINE):
        raise SystemExit(f"Missing files/systemd group merging in {nsswitch}")


def check_password_pam(system_auth: Path, passwd: Path) -> None:
    # A leading '-' is valid PAM syntax and suppresses logging when a module
    # is missing. Current authselect profiles use it for pam_systemd_home.
    if not re.search(r"^[ \t]*-?password[ \t]+(?:\[[^\]\n]+\]|[^ \t\n\[\]#]+)[ \t]+"
                     r"pam_systemd_home\.so(?:[ \t]|$)",
                     system_auth.read_text(), re.MULTILINE):
        raise SystemExit(f"Missing homed password support in {system_auth}")
    if not re.search(r"^[ \t]*-?password[ \t]+(?:substack|include)[ \t]+system-auth(?:[ \t]|$)",
                     passwd.read_text(), re.MULTILINE):
        raise SystemExit(f"Missing system-auth password delegation in {passwd}")


def check_accounts_redirect(desktop: Path, release: str) -> None:
    if not release.endswith(".mertensia1"):
        raise SystemExit(f"Missing Mertensia Accounts redirect in GNOME Settings: {release}")
    entry = configparser.ConfigParser(interpolation=None)
    entry.read_string(desktop.read_text())
    if (entry["Desktop Entry"].get("Exec") != "/usr/bin/mertensia-accounts" or
            entry["Desktop Entry"].get("Name") != "Mertensia Accounts"):
        raise SystemExit(f"GNOME Users desktop entry does not redirect to Mertensia Accounts: {desktop}")


def main() -> None:
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
                "nfs-client-utils", "nfsv3-client-utils", "nfsv4-client-utils", "rpcbind", "avahi",
                "oo7-daemon", "pam_oo7"}
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
    assert Path("/usr/share/mertensia/homed-accounts-only").is_file()
    release = subprocess.check_output(["rpm", "-q", "--qf", "%{RELEASE}", "accountsservice"], text=True).strip()
    if not release.endswith(".mertensia2"):
        raise SystemExit(f"Missing encrypted-account integration in accountsservice: {release}")
    settings_release = subprocess.check_output(
        ["rpm", "-q", "--qf", "%{RELEASE}", "gnome-control-center"], text=True).strip()
    check_accounts_redirect(Path("/usr/share/applications/gnome-users-panel.desktop"), settings_release)
    assert not Path("/usr/share/applications/org.gnome.Tour.desktop").exists()
    assert not Path("/usr/share/dbus-1/services/org.gnome.Tour.service").exists()
    assert Path("/usr/lib/systemd/systemd-cryptsetup").is_file()
    assert Path("/usr/lib64/cryptsetup/libcryptsetup-token-systemd-tpm2.so").is_file()
    subprocess.run(["authselect", "check"], check=True)
    check_group_merging(Path("/etc/nsswitch.conf"))
    # Console password changes must still reach homed through PAM; Settings
    # redirects password management to Mertensia Accounts.
    assert Path("/usr/bin/passwd").is_file()
    check_password_pam(Path("/etc/pam.d/system-auth"), Path("/etc/pam.d/passwd"))
    for module in ("pam_mertensia_password.so", "pam_systemd_home.so", "pam_oo7.so"):
        assert (Path("/usr/lib64/security") / module).is_file()
    service = Path("/usr/lib/pam.d/mertensia-password")
    assert [line.split() for line in service.read_text().splitlines() if line and not line.startswith("#")] == [
        ["password", "requisite", "pam_mertensia_password.so"],
        ["password", "[success=ok", "default=die]", "pam_systemd_home.so"],
        ["password", "optional", "pam_oo7.so", "use_authtok"],
    ]
    print(f"Desktop policy verified: {len(packages)} packages; masks, boot links, D-Bus, Polkit and authselect checked.")


if __name__ == "__main__":
    main()
