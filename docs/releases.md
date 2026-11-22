# Releases and updates

## Trust and credentials

The release job requests a GitHub OIDC token with `id-token: write`. Cosign
uses it to get a short-lived certificate from Sigstore's Fulcio service and
logs the signature in Rekor. The expected issuer is
`https://token.actions.githubusercontent.com`. The trusted workflow identity is
`https://github.com/MertensiaOS/MertensiaOS/.github/workflows/release.yml@refs/heads/main`.
The workflow needs no repository signing secret. Restrict the `release`
environment to `main` and require review of changes to the workflow.

The payload ships a Fedora Skopeo build with support for matching the Fulcio
Build Signer URI. Its policy at `/etc/containers/mertensia-bootc-policy.json`
trusts that workflow only for `ghcr.io/mertensiaos/mertensiaos`, using the
checked-in Fulcio root and Rekor public key from the
[public Sigstore trust root](https://github.com/sigstore/root-signing/blob/main/targets/trusted_root.json).
It rejects other registry pulls. The ordinary Podman policy rejects registry
pulls by default and allows the local container store for development.

Cosign 3.0.6 writes container signatures as legacy Sigstore attachments with
Rekor v1 timestamps so Skopeo can verify them during bootc pulls. The ISO
checksum file uses a Sigstore bundle. See the [Sigstore CI signing guide](https://docs.sigstore.dev/quickstart/quickstart-ci/)
and the [Skopeo verifier backport](https://github.com/podman-container-tools/container-libs/pull/625).

Allow the workflow's `GITHUB_TOKEN` to write packages. Both the official
GHCR package and `mertensiaos-integration` must be publicly readable by
unauthenticated clients. GHCR makes new packages private by default, so set
visibility when provisioning the packages. The integration package contains
instrumented test images. Apply a retention policy to its unique `run-*` tags.

## Automated publication

Release payload builds use `podman build --pull --no-cache`, refreshing RPMs
including Brave Origin even when the Fedora base digest is unchanged. Publication
still requires a manual dispatch. The finished payload is then repacked with
digest-pinned Chunkah into up to 128 content-based layers, reducing update
downloads. This converts the inherited OSTree image to plain bootc while
preserving its bootc metadata and release trust configuration. See the
[Chunkah measurements and implementation](chunkah.md).
Complete the graphical and live Safe Browsing
[Brave Origin acceptance checks](brave.md#acceptance-checks) before promoting a
release; the headless VM probe does not cover these services.

Run **Publish signed release** from GitHub Actions with a new version, such as
`45.20261005.1`, and channel `testing` or `latest`, from `main`. The workflow
requires the source tests and payload build first, then:

1. Builds, rechunks and validates a clean payload, then builds separately published, signed update and recovery fixtures from that rechunked payload.
2. Exercises actual ISO installation, recovery boot, accounts, signed upgrade and rollback, then validates the clean payload, rejecting development/test access and altered trust configuration.
3. Publishes a new version tag, signs its immutable digest and verifies it with both Cosign and containers/image.
4. Promotes the verified digest to the selected channel.
5. Builds the production ISO from that signed digest and uploads the ISO, checksum file and signed bundle as workflow artifacts.

It refuses an existing version and serializes workflow releases. A failed
signature or policy check leaves the update channel untouched; an incomplete
version publication requires a new version on retry. A failure building the ISO
after promotion leaves the verified payload published, so inspect the run before
retrying. Workflow artifacts are retained for 30 days; download approved releases
to your long-term distribution location.

## Verification

Install Cosign 3.0.6 and Python 3. Verify a payload by its actual digest:

```sh
bash scripts/verify-release.sh ghcr.io/mertensiaos/mertensiaos@sha256:REPLACE_WITH_64_HEX_DIGEST
```

This verifies the Cosign signature against the release workflow identity.
The publisher also checks the digest using the Skopeo verifier shipped in the
OS before moving the channel. Production ISO builds verify the signature and
then import the same immutable digest into local container storage.

For a downloaded ISO, use the verifier from a trusted project checkout. From
the directory containing the three artifacts, pass the checksum file to the
script, then verify the ISO bytes:

```sh
bash /path/to/trusted/checkout/scripts/verify-release.sh --checksums SHA256SUMS
sha256sum --check SHA256SUMS
```

## Upgrade and rollback

Installations that trust the previous Cosign public key need a transition
image that contains the new bootc policy and carries both signatures. Before
promoting a keyless-only release to a channel used by these installations,
publish and verify a dual-signed transition digest and let users boot into it.
Keep that digest available for older deployments that return via rollback.
If the previous signing key is unavailable, reinstall from a verified keyless
ISO after backing up user data.

On an installed production system:

```sh
sudo bootc status
sudo bootc upgrade --check
sudo bootc upgrade
sudo systemctl reboot
```

`upgrade` stages the image for the next boot. bootc verifies registry pulls
against `/etc/containers/mertensia-bootc-policy.json`. The installed update
reference follows the selected signed channel. A digest target remains pinned until explicitly
switched. The bootc timer stages signed updates daily on unmetered connections;
users choose when to restart. See [automatic updates](updates.md) for controls,
GNOME Software permission review and recovery acknowledgement.

Greenboot evaluates local desktop readiness after an update and can initiate
automatic retry reboots and rollback. This does not schedule update downloads.
See [boot health](boot-health.md) for the checks, notifications and recovery limits.

Before a manual rollback, disable automatic staging so it cannot replace the
rollback with another upgrade. Then return to the previous deployment:

```sh
sudo systemctl disable --now bootc-fetch-apply-updates.timer
sudo bootc rollback
sudo systemctl reboot
```

Re-enable the timer when ready with
`sudo systemctl enable --now bootc-fetch-apply-updates.timer`. After automatic
Greenboot recovery, also run `sudo mertensia-update resume` when the problem has
been resolved; this acknowledgement does not itself download or restart.

If a new deployment cannot boot, choose the previous deployment in the firmware
bootloader menu. Rollback does not undo changes to `/var` or encrypted homes;
keep separate user-data backups. See [recovery](recovery.md) if firmware or
Secure Boot changes cause the TPM to request the root recovery key.

The release workflow additionally requires signed persistent/transient failure
fixtures, automatic rollback, GRUB fallback, recovery-event publication and
bounded first-install/unhealthy-fallback retries. See
[automatic recovery tests](testing.md#automatic-greenboot-recovery).

The release workflow requires the integration harness's signed update and
rollback checks before promoting a channel. Ordinary offline CI ISO runs
explicitly skip that coverage; the separate public signed fixture supplies it
for a release.
