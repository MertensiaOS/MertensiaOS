# MertensiaOS
[![Discord](https://img.shields.io/discord/1546401028751233094)](https://discord.gg/BXJByrmMr)

MertensiaOS is a Fedora 45 bootc desktop with GNOME, an encrypted system volume,
TPM-backed root unlock, and separate encrypted homes managed by systemd-homed.

Fedora Chromium is the default browser, with native Wayland and sandboxing,
enforced site isolation, and reduced optional data collection. Chromium updates
arrive through OS releases and require a restart. See [browser hardening](docs/chromium.md)
for defaults, diagnostics and compatibility checks.

GNOME Software discovers, installs and automatically updates Flatpak apps from
Flathub. Updates requesting additional permissions stay pending for manual review
in Software. OS updates use bootc: signed updates download daily on unmetered
connections and are staged for your next restart. A desktop notification tells
you when an OS update is ready; automatic staging never requests a restart.
See [automatic updates](docs/updates.md) for controls and permission review.

Greenboot checks local storage and core desktop services after boot and can
automatically restore the previous OS deployment when an update fails. Boots
work offline, and users receive a desktop notification after automatic recovery.
See [boot health](docs/boot-health.md) for checks, limits and diagnosis.

The installer erases a whole disk. Supported targets are x86_64 computers with
UEFI, Secure Boot enabled, a usable TPM 2.0, and a disk of at least 24 GiB.
The project is under development; a passing unit suite does not establish that
a particular image boots on your hardware.

See [codebase architecture](docs/architecture.md) for the source layout, image
roles and application boundaries.

## Build a development image

Use a Linux x86_64 build host with rootful Podman, Bash, and enough disk space
for the payload, installer and ISO. Fedora is the supported build environment.
The ISO builder uses privileged containers and the rootful container store.

```sh
sudo dnf install podman bash
sudo bash scripts/iso-image.sh
```

The ISO is written to `output/installer/mertensiaos-installer.iso`. Development
images use a local update reference; they are intended for local testing.
For a direct development VM disk with a `testing` account, run
`sudo bash scripts/vm-image.sh`. That disk bypasses the ISO installer and is
never a release payload.

See [build configuration](docs/building.md) for image overrides, production
builds, signed payload requirements and build prerequisites.

## Run checks

On Fedora, install the desktop libraries used by the UI and tests:

```sh
sudo dnf install python3 python3-gobject python3-cairo librsvg2 python3-dbus python3-pyyaml gjs gtk4 libadwaita openssl \
  libxcrypt systemd iso-codes xkeyboard-config kbd tzdata
python3 scripts/check-source.py
python3 tests/run_tests.py
```

The test runner fails if required tests are skipped. The CI workflow also builds
and lints the payload. [VM integration tests](docs/testing.md) build and boot
the actual installer with Secure Boot and a software TPM, using disposable
virtual disks. UI previews are documented in [branding](branding/README.md).
The shared Quiet Bloom boot splash, native unlock prompts, and graphical boot
acceptance checks are documented in [Plymouth](docs/plymouth.md).

## Install, update and recover

Install from the ISO, save the root recovery key outside the live system, then
restart and create your first administrator account. The account password
unlocks its encrypted home; the root recovery key does not unlock that home.

See [recovery](docs/recovery.md) for interrupted installation, root unlock,
TPM resealing and home backups. See [releases and updates](docs/releases.md)
for signing credentials, signature verification, upgrades and rollback.

## License

MertensiaOS's own source code is [MIT licensed](LICENSE). Fedora packages and
other bundled components retain their own licenses; the Figtree font is
covered by its [SIL Open Font License](branding/fonts/OFL.txt).
