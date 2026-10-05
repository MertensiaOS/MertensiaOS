# Testing MertensiaOS

Run the unit suite from the repository root with the system Python that provides
PyGObject and D-Bus:

```sh
python3 tests/run_tests.py
```

The GTK preview instructions in `installer/branding/README.md` exercise layouts
without creating users, changing settings or installing to disks. Unit tests and
previews do not prove that an ISO boots or an installed encrypted system unlocks.

## Automated ISO integration

`scripts/test-iso.sh` builds and boots the actual `bootc-generic-iso` installer in
QEMU, runs the real installer backend against a newly created 40 GiB qcow2 disk,
and boots that installed disk without the ISO. UEFI firmware has Secure Boot
enabled with enrolled keys; a persistent swtpm supplies TPM 2.0 across reboots.
No host block devices or writable host directories are exposed to the VM.

The integration images layer a fixed test agent onto the production payload and
installer. The agent requires the dedicated QEMU firmware opt-in, virtio channel
and disk serial, and refuses guests with another writable disk. Its commands
are fixed test operations, not a shell. Production Containerfiles never copy
this agent or its service, and production image validation rejects either file.
Never publish an integration image as a production release.

Required host packages are QEMU (`qemu-system-x86_64` and `qemu-img`), swtpm,
enrolled-key OVMF firmware, and Podman for builds. KVM accelerates the run when
available; TCG works more slowly. Allocate at least 12 GiB of guest RAM by default
because the live installer stages the payload in its temporary filesystem, and
allow substantial free disk space for images, containers and artifacts.

```sh
# Read-only capability inventory. Does not build images or start a VM.
scripts/test-iso.sh --check

# Inspect planned container and QEMU commands without writing files.
scripts/test-iso.sh --dry-run --build

# Build isolated test overlays, produce the real ISO, and run all core checks.
sudo scripts/test-iso.sh --build \
  --work-dir /tmp/mertensia-iso-run --junit /tmp/mertensia-iso-run/junit.xml

# Reuse a production payload already in rootful Podman storage.
sudo scripts/test-iso.sh --build --accel kvm --base-image localhost/mertensiaos:base \
  --work-dir /tmp/mertensia-iso-next

# Building an ISO alone requires root; booting a prebuilt integration ISO does not.
sudo scripts/test-iso.sh --build-only --work-dir /tmp/mertensia-iso-build
scripts/test-iso.sh --iso /tmp/mertensia-iso-build/iso/mertensiaos-integration.iso \
  --work-dir /tmp/mertensia-iso-boot --junit /tmp/mertensia-iso-boot/junit.xml
```

`--work-dir` must be a new directory. Firmware variables, TPM state, the target
disk, build/QEMU/serial logs and `result.json` remain there for diagnosis. The
harness never deletes or reuses an existing disk. Remove disposable work
directories and the image tags listed in `images.json` after reviewing results.
When launched through `sudo`, the harness gives the invoking user ownership of
the work directory and known diagnostic reports/logs after all VM processes and
report writes finish. Private TPM state retains its original ownership; cleanup
of root-built artifacts may still require `sudo`.
`--junit` must be a regular report file inside the new work directory; report
paths cannot traverse symlinks or target host devices.
Recovery keys and account passwords stay in process memory and are redacted
from JSON/JUnit reports; completed test disks contain only disposable accounts.

Use `--firmware-json /path/to/enrolled-secure-boot.json` to select a QEMU firmware
descriptor explicitly. On systems without a suitable descriptor, pass both
`--ovmf-code /path/to/OVMF_CODE_4M.secboot.fd` and
`--ovmf-vars /path/to/OVMF_VARS_4M.ms.fd`; these explicit overrides are raw files.
A blank variables template will fail the guest's actual Secure Boot check.
`--accel tcg` forces software emulation, while `--accel kvm` requires accessible
`/dev/kvm`. Increase `--boot-timeout` and `--operation-timeout` for slow hosts.
Use explicit `--accel kvm` for a repeat on hosts with KVM access: it fails early
instead of falling back to CPU-intensive software emulation. Auto mode retains
the fallback for hosts without KVM and announces it before a run starts.
The integration-only live ISO uses the serial console with boot messages enabled.
Agent prerequisite failures appear in the virtio handshake and serial log;
timeouts identify the boot stage and the diagnostic files to inspect. The normal
production ISO keeps its regular graphical boot configuration.

The core run asserts:

1. The real live installer accepts x86_64 UEFI, enabled Secure Boot and TPM 2.0,
   and sees the disposable test disk.
2. The actual installation completes its recovery confirmation, removes the
   temporary key and cleans up target mounts/mappings.
3. The installed disk boots with persistent TPM state, root is encrypted,
   automatic TPM unlock succeeds, the saved recovery key independently unlocks
   root, installed SELinux enforces, and GDM is active.
4. The real account helper creates an initial administrator and an additional
   non-administrator with LUKS/ext4 homes. Both authenticate through the actual
   `gdm-password` PAM stack, reject incorrect passwords and appear in
   AccountsService's login cache.
5. Setup retirement disables autologin and revokes the setup caller's access.
   Root unlock, account records and authentication continue working after reboot.
6. A second boot uses fresh TPM state so the original sealed token cannot unlock
   root. The harness answers the actual initramfs recovery prompt over a test-only
   serial console, checks that the original token remains unusable and the
   recovered system's accounts work, then restores the original TPM state and
   verifies automatic unlock again.

The fixture invokes the account helper from its trusted root test agent with
the actual caller UID. This tests helper lifecycle and PAM integration; graphical
login sessions and the UI's polkit authorization require the interactive checks
below. Their coverage is explicitly marked skipped in machine-readable reports.

## Signed updates and rollback

Supply `--upgrade-image` with a distinct signed integration payload to exercise a
real `bootc switch --enforce-container-sigpolicy`, reboot into the changed digest,
check root/home authentication, roll back, reboot and verify the original digest
and accounts. Before the successful update it temporarily supplies an unrelated
public verification key for that exact test repository and proves signature
verification rejects the image without staging a deployment; original trust is
restored even if the check fails. Without that option, the report marks
update/rollback as skipped.
Use `--require-upgrade` when CI or release acceptance requires this coverage.

The update must include the same test agent/service and payload role, be signed,
and be reachable from the guest's QEMU user network. For a separate test registry
repository, embed its public verification key in the disposable overlay:

```sh
sudo scripts/test-iso.sh --build --require-upgrade \
  --upgrade-image ghcr.io/YOUR_ORG/mertensia-integration:next \
  --upgrade-public-key /path/to/integration-signing.pub \
  --work-dir /tmp/mertensia-iso-upgrade
```

The overlay adds a `sigstoreSigned` rule with `matchRepository` for that single
integration repository and enables Sigstore attachment discovery. It preserves
the production default-reject policy and cannot replace existing trust or the
production repository's key. Only a PEM **public** key is accepted. Build and
sign the distinct update overlay separately with `tests/integration/Containerfile`
and `INTEGRATION_ROLE=payload`, using a different `INTEGRATION_REVISION` value
so its deployment digest changes. The test registry must provide its signature
attachments. This harness does not push or sign images, alter registry access,
or insert private signing keys into guests.

The release workflow prepares a signed update fixture in the separate public
`ghcr.io/mertensiaos/mertensiaos-integration` repository and requires this complete
update/rollback sequence before publishing the clean production payload. Routine
offline ISO runs can omit it; that omission remains visible as skipped coverage.

## Interactive acceptance

Before releasing, also boot the uninstrumented production ISO in a disposable
UEFI/Secure Boot/TPM VM or test machine and complete the graphical workflow.
The dry-run QEMU plan documents the firmware, TPM and fresh file-disk setup;
use a visible display and the normal production ISO for these checks.

1. Confirm the correct disk is shown, erasure requires explicit confirmation,
   installation progress is legible, and the recovery key can be saved before
   restart. Verify the live installer cannot select mounted/in-use disks.
2. Remove the ISO, boot the installed disk, complete first-boot setup through
   the UI and check regional settings, keyboard input and timezone.
3. Log in through GDM, log out, and log in again. Check the encrypted home is
   usable, the initial account has administrator access, and setup is no longer
   offered. Verify the account UI's real polkit prompt and denial behavior.
4. Add a non-administrator, log in as that account, and confirm it cannot create
   administrators. Check both users remain visible after reboot.
5. On a disposable disk, test root recovery after changing Secure Boot/TPM state,
   reseal the root volume, and verify automatic unlock on the next boot. Test
   suspend/resume and encrypted-home recovery separately on supported hardware.

Record the tested image digest, firmware/TPM versions and pass/fail results.
Automated PAM success does not establish that a full GNOME desktop session starts
correctly. The forced recovery boot tests the real initramfs prompt through the
serial console; verify the production image's graphical recovery prompt manually.
