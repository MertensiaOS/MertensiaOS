# Testing MertensiaOS

Run the unit suite from the repository root with the system Python that provides
PyGObject and D-Bus:

```sh
python3 tests/run_tests.py
```

The GTK preview instructions in `branding/README.md` exercise layouts
without creating users, changing settings or installing to disks. Unit tests and
previews do not prove that an ISO boots or an installed encrypted system unlocks.
The [Plymouth guide](plymouth.md) covers native renderer previews and the optional
`--graphical-boot` ISO run, which retains production splash arguments and answers
the actual encrypted-root recovery prompt through a private QMP keyboard.

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
available; TCG works more slowly. The default guest RAM allocation is 12 GiB.
The live installer uses the encrypted target disk for bulk image-export scratch
data and checks target capacity for both the deployment and temporary data.
Scratch files are removed before the target is unmounted. Guest RAM is used by
the live desktop and installation processes; adjust it with `--memory-mib`.
Allow substantial free disk space for images, containers and artifacts.
VM artifacts default to `/var/tmp`; choose a disk-backed `--work-dir` rather
than a RAM-backed temporary filesystem for large ISOs and virtual disks.

```sh
# Read-only capability inventory. Does not build images or start a VM.
scripts/test-iso.sh --check

# Inspect planned container and QEMU commands without writing files.
scripts/test-iso.sh --dry-run --build

# Build isolated test overlays, produce the real ISO, and run all core checks.
sudo scripts/test-iso.sh --build \
  --work-dir /var/tmp/mertensia-iso-run --junit /var/tmp/mertensia-iso-run/junit.xml

# Reuse a production payload already in rootful Podman storage.
sudo scripts/test-iso.sh --build --accel kvm --base-image localhost/mertensiaos:base \
  --work-dir /var/tmp/mertensia-iso-next

# Building an ISO alone requires root; booting a prebuilt integration ISO does not.
sudo scripts/test-iso.sh --build-only --work-dir /var/tmp/mertensia-iso-build
scripts/test-iso.sh --iso /var/tmp/mertensia-iso-build/iso/mertensiaos-integration.iso \
  --work-dir /var/tmp/mertensia-iso-boot --junit /var/tmp/mertensia-iso-boot/junit.xml
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

The installed-system run also requires the unprivileged headless Chromium probe:
effective policies, native namespace/seccomp sandboxing and active site
isolation. It opens no debugging TCP port. Graphical desktop integration,
two-account browser data persistence and live Safe Browsing checks remain
[explicit interactive acceptance checks](chromium.md#acceptance-checks).

The core run asserts:

1. The real live installer accepts x86_64 UEFI, enabled Secure Boot and TPM 2.0,
   and sees the disposable test disk.
2. The actual installation completes its recovery confirmation, removes the
   temporary key and cleans up target mounts/mappings.
3. The installed disk boots with persistent TPM state, root is encrypted,
   automatic TPM unlock succeeds, the saved recovery key independently unlocks
   root, installed SELinux enforces, and GDM is active.
   Greenboot passes before any accounts exist. A later reboot with networking
   disabled must also pass; networking is restored before registry tests.
4. The real account helper creates an initial administrator and an additional
   non-administrator with LUKS/ext4 homes. Both authenticate through the actual
   `gdm-password` PAM stack, reject incorrect passwords and appear in
   AccountsService's login cache.
   Enumeration is also checked after restarting AccountsService with at least
   64 shadow entries, including system accounts, to cover the homed user-limit
   regression and GDM's system-account/locked-account filters.
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
  --work-dir /var/tmp/mertensia-iso-upgrade
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

## Automatic updates and Flatpak permissions

Run the stock GNOME Software permission checks against a built payload:

```sh
sudo bash scripts/test-flatpak-updates.sh localhost/mertensiaos:base
```

The script builds a disposable container with a private Xvfb display, D-Bus and
a local Flatpak repository; it exposes no host mounts. The fixture repository
uses unsigned local test content and a tiny runtime so no SDK is downloaded.
It installs three apps in both system and user scopes, publishes updates with
unchanged, reduced and expanded permissions, and checks the actual GNOME
Software Flatpak plugin's `new-permissions` classification. It verifies declining
an expanded-permission update leaves the installed commit unchanged, then applies
the unrelated updates without installing the held app. CI requires this check.
The container's test files are rejected by production image validation.

These checks do not run the real logged-in user's GNOME background scheduler or
prove the visual review flow. In an ordinary GNOME session, verify Software starts
at login, automatically applies unchanged/reduced-access updates, and leaves
expanded-access updates pending. Open a flagged app's update details and check
that the added access is clearly described. Cancel and verify the old version
remains. Then manually update the app. Stock **Update All** can approve such
updates without forcing the details open; this is the chosen native behavior.
Also verify the update preferences, metered-network deferral, and that existing
Flatpak overrides and portal permissions remain intact.

For actual automatic OS staging, add `--automatic-update-image` to the signed
ISO suite, specifying a tagged healthy fixture in the same integration repository
as `--upgrade-image`. The tag must resolve to the healthy fixture's signed digest.
Release CI creates a unique signed fixture tag and requires this path. For example:

```sh
sudo scripts/test-iso.sh --build --require-upgrade \
  --upgrade-image ghcr.io/YOUR_ORG/mertensia-integration@sha256:REPLACE_WITH_DIGEST \
  --automatic-update-image ghcr.io/YOUR_ORG/mertensia-integration:healthy \
  --upgrade-public-key /path/to/integration-signing.pub \
  --work-dir /var/tmp/mertensia-automatic-updates
```

The guarded guest operation temporarily points only the disposable booted
deployment's origin at that signed tag, invokes the unmodified production update
service, and restores the origin even on failure. It checks signature policy,
the staged digest, desktop status and unchanged boot identity, then the existing
suite boots the update and verifies accounts and rollback. Recovery fixtures also
verify staging pauses after both GRUB fallback and Greenboot rollback, and that
administrator acknowledgement does not itself change deployments. Offline runs
without the tagged fixture explicitly report automatic staging as skipped.

In a normal GNOME session, also verify the OS restart-ready notification, one
delivery per digest/user, stale-notice clearing after boot, and the explanation
of paused OS updates after recovery. These visual checks remain release
acceptance requirements.

## Automatic Greenboot recovery

Build and sign three distinct integration overlays in the same separate
integration repository, using the same public key. Set
`INTEGRATION_HEALTH_MODE=none` for the healthy update, `always` for a persistent
failure and `transient` for a check that fails once per fixture revision. Set a
distinct `INTEGRATION_REVISION` for each image. These hooks invoke the guarded
test agent and must never be included in a production payload.

```sh
sudo scripts/test-iso.sh --build --base-image localhost/mertensiaos:release \
  --upgrade-image ghcr.io/YOUR_ORG/mertensia-integration:healthy \
  --greenboot-failure-image ghcr.io/YOUR_ORG/mertensia-integration:unhealthy \
  --greenboot-transient-image ghcr.io/YOUR_ORG/mertensia-integration:transient \
  --upgrade-public-key /path/to/integration-signing.pub \
  --require-upgrade --require-greenboot-recovery \
  --work-dir /var/tmp/mertensia-greenboot-test
```

`--require-greenboot-recovery` rejects missing fixtures. The release workflow
publishes these disposable signed fixtures, requires this suite, then validates
and publishes only the clean production payload.

The suite checks first-install failure without a rollback deployment, a
transient failure that recovers on the same update, automatic rollback after a
persistent failure, and exhaustion when the fallback is also unhealthy. Fixed
test operations install/remove a local failing check for the first-install and
unhealthy-fallback cases. A stopped guest must report manual intervention;
timeouts, unexpected success and unlimited rebooting fail acceptance.

For pre-userspace fallback, the agent stages the signed healthy fixture, invokes
the upstream rollback-trigger operation and explicitly finalizes the deployment.
It temporarily changes only that deployment's BLS `linux` directive to a
nonexistent test path. GRUB must fall back, and Greenboot must make that fallback
permanent and publish a current-boot recovery event. The test restores the BLS
directive afterward; signed kernel bytes are never changed. This exercises a
GRUB load failure, not a kernel hang or panic.

QEMU exits on each guest reboot. The host starts the next attempt only after a
clean exit, preserving the disk, enrolled firmware variables and TPM identity.
Recovery follows at most eight automatic reboots and never forces a reset of a
running unhealthy guest. Each installed boot has its own numbered QEMU/serial
logs; successful recovery summaries include boot identities and Greenboot
journals. The suite also verifies root/home authentication and recovery-event
metadata after rollback. Missing automatic-recovery coverage is explicitly
reported as skipped on ordinary runs without fixtures.

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
6. Trigger an automatic rollback with disposable integration fixtures, then log
   into GNOME. Confirm the recovery notification appears with the intended text.
   Log out/in and reboot: that user must not receive the same event again. A
   second user must receive it once. Confirm normal boots, healthy updates and
   the setup session stay quiet. Temporarily stop the session notification
   service and verify delivery retries when it becomes available. The automated
   suite tests event publication and per-user delivery logic, not GNOME visuals.

Record the tested image digest, firmware/TPM versions and pass/fail results.
Automated PAM success does not establish that a full GNOME desktop session starts
correctly. The forced recovery boot tests the real initramfs prompt through the
serial console; verify the production image's graphical recovery prompt manually.
