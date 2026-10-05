# Building MertensiaOS

Build on Linux x86_64 using rootful Podman. The container build installs packages
from Fedora repositories, and the ISO builder needs networking, privileged
container execution, loop devices and access to `/var/lib/containers/storage`.
Install Podman with `sudo dnf install podman`. Build artifacts belong under
`output/`; they are ignored by Git. Reserve ample space for image layers and
the ISO; 30 GiB free is a practical starting point.

The payload `Containerfile` configures GNOME, PAM/systemd-homed, first-boot setup,
initramfs encryption support and the release signature policy. The live image
in `Containerfile.installer` adds the installer and image-builder contract.
`Containerfile.dev` adds a testing account and must never be published.

## Development

```sh
sudo bash scripts/installer-image.sh  # payload and live container
sudo bash scripts/iso-image.sh        # containers plus generic installer ISO
sudo bash scripts/vm-image.sh         # direct 12 GiB testing disk, not an ISO test
```

The direct VM disk has user `testing`, password `mertensia`, and wheel membership.
It exists only for development. A normal installer target must be at least
24 GiB and receives TPM-backed LUKS root encryption.

The payload rebuilds Fedora's AccountsService source RPM in a separate build
stage with `system/patches/accountsservice-homed-enumeration.patch`. Upstream 26.27.3
counts system accounts in `/etc/shadow` toward the 50-user homed enumeration
limit, hiding all encrypted-home users on this image. The patch counts the
enumerated users instead. Both daemon and client RPMs retain Fedora's packaging
with a `.mertensia1` release suffix; build tools stay in the build stage. Review
and remove this patch once Fedora ships the corrected enumeration check.

| Variable | Default / meaning |
| --- | --- |
| `MERTENSIA_BUILD_MODE` | `development`; set `production` for a signed release |
| `MERTENSIA_PAYLOAD_IMAGE` | `localhost/mertensiaos:base`; production requires an official GHCR digest |
| `MERTENSIA_INSTALLER_IMAGE` | `localhost/mertensiaos:installer` |
| `MERTENSIA_TARGET_IMAGE` | Development: payload reference; production: `ghcr.io/mertensiaos/mertensiaos:latest` |
| `MERTENSIA_SKIP_BASE_BUILD` | `1` reuses an existing development payload |
| `MERTENSIA_BUILDER_IMAGE` | Digest-pinned osbuild image-builder in `scripts/iso-image.sh` |

Pass overrides through `sudo env`, because sudo may remove shell environment
variables. The ISO script invokes the installer-container build automatically.

## Production

Publish and sign the payload first, as described in [releases](releases.md).
Use its immutable digest as the embedded payload and a signed channel tag as
the installed update target:

```sh
sudo env MERTENSIA_BUILD_MODE=production \
  MERTENSIA_PAYLOAD_IMAGE=ghcr.io/mertensiaos/mertensiaos@sha256:REPLACE_WITH_64_HEX_DIGEST \
  MERTENSIA_TARGET_IMAGE=ghcr.io/mertensiaos/mertensiaos:latest \
  bash scripts/iso-image.sh
```

Replace the digest placeholder with the actual release digest. Production mode
verifies the signature using the checked-in public key and the containers/image
policy before importing the image into the local store. It refuses a local or
foreign payload and does not rebuild the verified payload. Production installs
also check target reachability before erasure and use bootc's enforced signature
policy and fetch check. Networking is therefore required for production
installation, even though the payload is embedded in the ISO.

The image-builder container remains pinned by digest. Update that pin deliberately
and run the ISO tests before accepting a builder update. The Fedora base tag and
package repositories receive upstream changes; every release records its payload
digest rather than claiming byte-for-byte rebuilds from moving upstream inputs.

## Fresh-checkout validation

```sh
python3 scripts/check-source.py
python3 tests/run_tests.py
sudo podman build --pull -f Containerfile -t localhost/mertensiaos:base .
sudo bash scripts/validate-image.sh localhost/mertensiaos:base
```

All `COPY` inputs must be tracked in Git. Commit source, configuration, branding
and tests together; do not commit `output/`, local container stores, private
signing keys or credentials.
