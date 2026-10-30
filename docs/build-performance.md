# Local ISO build performance

Measured on 30 October 2026 using the local 28-CPU, 31 GiB machine and the
repository's pinned image-builder. Podman used overlay storage on Btrfs with
`metacopy=on` and reported `Native Overlay Diff: false`.

| Workload | Measured time |
| --- | ---: |
| Payload configuration rebuild with the previous serial file layers | 336 s |
| Payload configuration rebuild with batched file installation | 34 s |
| Installer rebuild on the new payload | 30 s |
| Full ISO assembly | 93 s |
| Entire `scripts/iso-image.sh` run, including rebuilding both containers | 159 s |
| Unchanged repeat run, including image checks and complete ISO checksum | 6 s |
| Payload rebuild after a one-file Python UI edit | 14 s |

The package installation and AccountsService source-RPM stages were cached in
these measurements. These are development rebuild timings, not fresh-machine
download and package-install benchmarks. The original approximately 13-minute
build was user reported and was not captured with stage timings. Results vary
with upstream base changes, repository downloads, storage and CPU load.

## Causes and changes

Small uncached file layers on the complete OS took approximately 4.0–4.6 seconds
each on this storage configuration. Even staging individual files on an empty
filesystem cost approximately one second per layer. The new layout collects
source directories with a few COPY instructions, then installs the declared
files and configures the OS in one layer. The file mappings live in
`scripts/payload-files.json` and `scripts/installer-files.json`.

The same configuration command blocks run through mounted build scripts. Each
block has its own failure boundary, matching the previous separate RUN commands.
The build scripts and input directories are not installed into the finished OS.
Python application modules are added after configuration and initramfs generation,
allowing UI changes to reuse those expensive layers. The 14-second UI probe used
a temporary build context and a separate image tag; it did not edit working files.

Independent container stages build in parallel, with four jobs by default.
Installer RPM downloads and metadata use a persistent Podman cache. Image-builder
also receives persistent store and RPM metadata directories. The ISO script
reuses an existing output only when its immutable image inputs and recipe match
and its complete SHA-256 checksum verifies. Set `MERTENSIA_FORCE_ISO_BUILD=1` to
force assembly while retaining container and download caches.

SquashFS compression, payload export and container deployment accounted for most
of the remaining ISO assembly time. Their compression format and boot behavior
remain unchanged.

## Validation and reproducing measurements

All 267 unit and host integration tests passed. Source syntax and build inputs,
payload validation, initramfs contents, BIOS and UEFI ISO boot entries were
checked. A comparison with the original Containerfiles confirmed that all 64
payload and 12 installer source-file mappings, and all 13 payload and seven
installer configuration command blocks, were preserved.

The real VM installation suite also passed with Secure Boot and TPM enabled:
encrypted installation, boot without installation media, enforcing SELinux, GDM,
Chromium sandboxing, encrypted accounts and PAM authentication, offline reboot,
forced initramfs recovery-key unlock and restoration of automatic TPM unlock.
The local report is `.build/build-optimization-integration/result.json`.
Signed update/rollback fixtures and visual acceptance checks are separate suites
and were not exercised by this run.

The scripts print payload, installer, assembly and total elapsed times. To measure
an unchanged repeat, run the normal ISO command twice. To measure assembly every
time:

```sh
sudo env MERTENSIA_FORCE_ISO_BUILD=1 bash scripts/iso-image.sh
```

Image-builder exports its detailed stage log and manifest beside the ISO.
Local measurement logs are under `.build/iso-final-build.log`,
`.build/iso-final-repeat.log`, and `.build/payload-source-edit.log`.
