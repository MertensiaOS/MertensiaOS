# MertensiaOS
[![Discord](https://img.shields.io/discord/1546401028751233094)](https://discord.gg/BXJByrmMr)

MertensiaOS is a Fedora 45 bootc desktop with GNOME, an encrypted system volume,
TPM-backed root unlock, and separate encrypted homes managed by systemd-homed.

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
sudo dnf install python3 python3-gobject python3-dbus gtk4 libadwaita openssl \
  libxcrypt systemd iso-codes xkeyboard-config kbd tzdata
python3 scripts/check-source.py
python3 tests/run_tests.py
```

The test runner fails if required tests are skipped. The CI workflow also builds
and lints the payload. [VM integration tests](docs/testing.md) build and boot
the actual installer with Secure Boot and a software TPM, using disposable
virtual disks. UI previews are documented in [branding](branding/README.md).

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
