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
checked-in Fulcio root, Fulcio issuing intermediate and Rekor public key from the
[public Sigstore trust root](https://github.com/sigstore/root-signing/blob/main/targets/trusted_root.json).
It rejects other registry pulls. The ordinary Podman policy rejects registry
pulls by default and allows the local container store for development.

Cosign 3.1.4 writes container signatures as legacy Sigstore attachments with
Rekor v1 timestamps so Skopeo can verify them during bootc pulls. These
attachments can omit the issuing certificate, so the local Fulcio bundle
includes it. The ISO checksum file uses a Sigstore bundle.
See the [Sigstore CI signing guide](https://docs.sigstore.dev/quickstart/quickstart-ci/)
and the [Skopeo verifier backport](https://github.com/podman-container-tools/container-libs/pull/625).

Allow the workflow's `GITHUB_TOKEN` to write packages. Both the official
GHCR package and `mertensiaos-integration` must be publicly readable by
unauthenticated clients. GHCR makes new packages private by default, so set
visibility when provisioning the packages. The integration package contains
instrumented test images. Apply a retention policy to its unique `run-*` tags.

## Upstream changes

**Check upstream build inputs** runs every four hours and supports manual dispatch
from `main`. Its report compares the AMD64 Fedora bootc digest, the stable Fedora
and Brave RPM inputs, Chunkah and Image Builder with the [build-input lock](building.md#build-input-lock).
It checks stable Bodhi advisories against the RPM inventory of signed `latest` and
the proposed payload. This includes packages inherited from Fedora bootc. The
report distinguishes fixes present in the proposed payload from advisories that
have yet to reach it. A failed upstream or registry request fails the workflow.

Review the run summary or download `upstream-input-report` for the complete package
and advisory diff. A changed resolution updates the single
`automation/refresh-build-inputs` PR. PRs that include a Fedora security fix receive
the `security` label. An unchanged resolution leaves Git and the release channels
as they are. Dependabot proposes GitHub Actions updates separately.

Install a private GitHub App on this repository with **Contents**,
**Pull requests** and **Issues** write access. Store its App ID as the Actions
variable `DEPENDENCY_REFRESH_APP_ID` and its private key as the Actions secret
`DEPENDENCY_REFRESH_APP_PRIVATE_KEY`. The workflow uses `GITHUB_TOKEN` to write
the RPM bundle and a short-lived app installation token to update the refresh PR.

GitHub signs the refresh commit on a temporary `automation/refresh-signing/*`
branch based on the resolved `main` revision. The workflow checks the signature,
parent and contents before updating `automation/refresh-build-inputs` with a Git
lease. Repository rules must allow the app to create and delete the temporary
branches and replace the refresh branch head.

The refresh verifies that both `ci.yml` and `iso-smoke.yml` start for the exact
PR head. Require the source and payload checks in the `main` ruleset. ISO smoke
runs on lock changes, and the release workflow independently requires the
installation suite.
[GitHub workflow-event authentication](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

## Automated publication

Merging a changed lock generates a candidate automatically. To build a candidate
manually, dispatch **Publish signed release** from `main` and leave
`promote_digest` empty. You can override the version with an unused OCI tag.
The default version includes the OS version, UTC date, workflow run number
and attempt.

1. The workflow verifies the upgrade baseline, builds a single locked payload and
   rechunks it with the pinned Chunkah container. It requires successful CI on the
   exact source commit and runs the existing installation, signed upgrade, and
   Greenboot gates on separate integration fixtures.
2. It pushes and signs the immutable candidate. The release VM boots the exact
   signed `latest` digest, stages and reboots into the candidate, and rolls back to
   the prior digest. If the repository has never published `latest`, the gate uses
   the existing signed `testing` digest. Signature failures and registry errors fail
   the gate.
3. It builds the production installer from the verified digest using the same RPM
   lock and includes `release.json` with source commit, lock revision, installed
   inventory, upgrade baseline and final OCI digest. The signed `SHA256SUMS` covers
   the ISO and this metadata.
4. The workflow advances `testing` only after these steps pass. An older run cannot
   replace a newer candidate, and a change to the stable baseline invalidates
   earlier candidates. Unsuccessful attempts can leave a signed, versioned tag
   for diagnosis.

### Promote to stable

Configure the GitHub `stable` environment for `main` with required maintainer
reviewers and disable self-review. The promotion job fails if reviewers are
missing. Before approving, record the tested digest, Brave RPM version and the
results of [hands-on Brave acceptance](brave.md#acceptance-checks).

To try the candidate, update a disposable installed machine to the verified
`testing` digest. For an installer targeting `testing`, check out the candidate's
source commit and build the ISO with `MERTENSIA_TARGET_IMAGE=ghcr.io/mertensiaos/mertensiaos:testing`.
The workflow's uploaded production ISO targets `latest` for stable distribution.

Dispatch **Publish signed release** from `main` with the current passing
`testing` SHA-256 in `promote_digest`. After the environment review, the job copies
that exact signed digest to `latest`. It checks that `testing` still points to the
approved candidate and that the upgrade baseline is still current. Promotion does
not rebuild the payload or ISO.

Workflow artifacts are retained for 30 days. Save approved installer artifacts and
their signed metadata for long-term distribution.

## Verification

Install Cosign 3.1.4 and Python 3. Verify a payload by its actual digest:

```sh
bash scripts/verify-release.sh ghcr.io/mertensiaos/mertensiaos@sha256:REPLACE_WITH_64_HEX_DIGEST
```

This verifies the Cosign signature against the release workflow identity.
The publisher also checks the digest using the Skopeo verifier shipped in the
OS before moving the channel. Production ISO builds verify the signature and
then import the same immutable digest into local container storage.

For a downloaded ISO, use the verifier from a trusted project checkout. From
the directory containing the installer, metadata, checksums and signature bundle, pass the checksum file to the
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
