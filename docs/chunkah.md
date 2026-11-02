# Chunkah release layers

Release builds and the CI payload job use Chunkah after building the payload.
Releases run the real installer/VM gate on the rechunked payload before
publication and signing. Development builds retain their normal cached layers.
The local trial on 30 October 2026 measured the benefit below.

## Measured results

Two existing payloads were compared: `localhost/mertensiaos:chromium`
(`183c257ff795`) and the later `localhost/mertensiaos:plymouth`
(`b7bc140b295f`). Both inherit Fedora base `1964978e682d`. This is a real
project-change comparison, not a Fedora package-update benchmark.

All sizes below are sums of gzip-compressed layer descriptor sizes. Update size
counts later-image layers whose digests are absent from the earlier image. This
estimates a layer-cached pull; it is not a measured network transfer and excludes
manifests, signatures and protocol overhead.

| Measurement | Original | Chunkah, 128 layers |
| --- | ---: | ---: |
| Earlier image | 2,344,348,523 bytes | 2,082,695,395 bytes |
| Later image | 2,505,008,928 bytes | 2,242,971,544 bytes |
| Update layer data | 1,444,812,576 bytes | 780,861,320 bytes |
| Later image layers reused | 66 of 147 | 98 of 128 |

Update layer data fell **46%**; the later image was **10.5% smaller**. The total
size saving includes flattening old layers and removing embedded OSTree data,
in addition to repacking. Chunkah took 12 seconds for the earlier image and
10 seconds for the later image on the local 28-CPU machine, with parallel gzip
compression. These times exclude pulling Chunkah, importing the result and
payload validation. Other package changes may produce different savings.

## Validation of the 30 October 2026 trial

- `scripts/validate-image.sh` passed on the rechunked payload, including fatal
  bootc lint, initramfs/Plymouth checks, application configuration and trust checks.
- A mounted filesystem comparison checked 81,237 paths in each image. Contents,
  modes, ownership, symlink targets and extended attributes matched outside
  `/sysroot`. Timestamps and SELinux labels were excluded from comparison.
- Repeating the transformation produced the same image ID and all 128 identical
  layer digests. OCI config was preserved except the two deliberately removed
  OSTree labels.
- Source syntax checks passed.

This historical trial covered image transformation and container validation;
it did not include installer/VM boot, signed upgrades or rollback. Those results
must not be used as release acceptance for another image. The release workflow
requires the installer and signed update/recovery suites on the rechunked payload
before publication; a failed rechunk, validation or VM gate stops the release.
See [testing](testing.md) for the required fixtures and interactive checks.

The trial's OCI layouts, timing logs and filesystem comparisons were local,
ignored build outputs, not files distributed with this repository. To reproduce
or publish a new measurement, retain the source/output digests, tool versions,
commands and reports with the matching workflow or issue. The trial tags below
are local examples and must be replaced with images in your own container store.

## Reproduce

Use the same Podman user/store that contains the source payload. The script
requires Podman, Skopeo and the normal payload validation dependencies, plus
space for a temporary compressed OCI layout and the imported image.

```sh
bash scripts/rechunk-image.sh localhost/mertensiaos:plymouth localhost/mertensiaos:chunkah-trial
```

For a rootful source image, run the script with sudo. It pins the source image ID,
preserves OCI metadata, uses a digest-pinned Chunkah v0.7.1 container, and imports
the result under the supplied output tag before running payload validation.
Non-RPM timestamps use a fixed epoch of zero for stable layer reuse.

Following the [upstream bootc recipe](https://github.com/coreos/chunkah/tree/v0.7.1#compatibility-with-bootable-bootc-images),
it converts the inherited OSTree image to plain bootc by pruning `/sysroot/` and
removing `ostree.commit` and `ostree.final-diffid`, while preserving
`containers.bootc=1`. Rechunking must precede integration testing, publishing
and signing: the transformation changes the image digest.
