# Releases and updates

## Trust and credentials

`cosign.pub` is the existing project release trust root. Images install it as
`/etc/pki/containers/mertensia.pub`. The container policy rejects registry pulls
unless they are signed images from `ghcr.io/mertensiaos/mertensiaos`. The only
unsigned exception is the privileged local container store used for offline
development payloads. This policy also applies to ordinary Podman pulls;
additional repositories require an explicit administrator policy change.

The registry configuration enables Sigstore attachments for the official
repository. Container signatures use Cosign 3.0.6's legacy attachment format
for compatibility with containers/image; checksum blobs use Sigstore bundles.
These choices follow the [containers/image signature policy documentation](https://github.com/containers/image/blob/main/docs/containers-policy.json.5.md)
and [Cosign signing documentation](https://github.com/sigstore/cosign/blob/v3.0.6/doc/cosign_sign.md).

The private key corresponding to `cosign.pub` is required. A new unrelated key
cannot sign updates accepted by existing installations. The publisher compares
the signing key's public half with the checked-in key before pushing. Never
commit private keys; keep them in protected secrets or a supported KMS.

Configure the repository's GitHub Actions `release` environment with:

| Secret | Purpose |
| --- | --- |
| `COSIGN_PRIVATE_KEY` | The encrypted private key matching `cosign.pub` |
| `COSIGN_PASSWORD` | Its passphrase, or empty for an unencrypted key |

Allow the workflow's `GITHUB_TOKEN` to write packages and ensure both the official
GHCR package and `mertensiaos-integration` test package are publicly readable.
The latter contains instrumented test images and must never be distributed as
an OS release. Apply a retention policy to its unique `run-*` tags.
Add environment protection appropriate to
your maintainers. Credentials and environment settings cannot be created from
source code; configure them before dispatching a release.

## Automated publication

Run **Publish signed release** from GitHub Actions with a new version, such as
`45.20261005.1`, and channel `testing` or `latest`, from `main`. The workflow
requires the source tests and payload build first, then:

1. Builds a clean payload and a separately published, signed update-test fixture.
2. Exercises actual ISO installation, recovery boot, accounts, signed upgrade and rollback, then validates the clean payload, rejecting development/test access and altered trust configuration.
3. Publishes a new version tag, signs its immutable digest and verifies it with both Cosign and containers/image.
4. Promotes the verified digest to the selected channel.
5. Builds the production ISO from that signed digest and uploads the ISO, signed checksum bundle and public key as workflow artifacts.

It refuses an existing version and serializes workflow releases. A failed
signature or policy check leaves the update channel untouched; an incomplete
version publication requires a new version on retry. A failure building the ISO
after promotion leaves the verified payload published, so inspect the run before
retrying. Workflow artifacts are retained for 30 days; download approved releases
to your long-term distribution location.

For local publication, install Podman, Skopeo, OpenSSL, Python and Cosign 3.0.6.
Run all commands under the same account/container store. Configure Podman/Skopeo
and Cosign to use the same Docker-format authentication file. For example, in a
root shell:

```sh
install -d -m 0700 /root/.docker
podman login --authfile /root/.docker/config.json ghcr.io
export REGISTRY_AUTH_FILE=/root/.docker/config.json
export MERTENSIA_SIGNING_KEY=/secure/location/cosign.key
podman build --pull -f Containerfile -t localhost/mertensiaos:release .
bash scripts/publish-image.sh localhost/mertensiaos:release 45.20261005.1 testing
```

Cosign prompts for an encrypted key's password unless `COSIGN_PASSWORD` is supplied
securely by your environment. Do not put passphrases in command arguments.

## Verification

Verify a payload by its actual digest:

```sh
bash scripts/verify-release.sh ghcr.io/mertensiaos/mertensiaos@sha256:REPLACE_WITH_64_HEX_DIGEST
```

This checks both the Cosign signature and the policy used by the OS. The second
check downloads the payload into temporary OCI storage, so allow enough space.
Production ISO builds use the same verifier to import directly into container storage.

For a downloaded ISO, obtain the public key through a trusted project checkout,
verify the checksum bundle, then verify the ISO bytes:

```sh
cosign verify-blob --key cosign.pub --bundle SHA256SUMS.sigstore.json SHA256SUMS
sha256sum --check SHA256SUMS
```

## Upgrade and rollback

On an installed production system:

```sh
sudo bootc status
sudo bootc upgrade --check
sudo bootc upgrade
sudo systemctl reboot
```

`upgrade` stages the image for the next boot. bootc verifies registry pulls
against `/etc/containers/policy.json`; the installed update reference follows
the selected signed channel. A digest target remains pinned until explicitly
switched. No automatic update/reboot timer is enabled by this project.

To return to the previous deployment:

```sh
sudo bootc rollback
sudo systemctl reboot
```

If a new deployment cannot boot, choose the previous deployment in the firmware
bootloader menu. Rollback does not undo changes to `/var` or encrypted homes;
keep separate user-data backups. See [recovery](recovery.md) if firmware or
Secure Boot changes cause the TPM to request the root recovery key.

The release workflow requires the integration harness's signed update and
rollback checks before promoting a channel. Ordinary offline CI ISO runs
explicitly skip that coverage; the separate public signed fixture supplies it
for a release.
