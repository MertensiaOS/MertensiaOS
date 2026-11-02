# MertensiaOS documentation

Start with the [project README](../README.md) for the development status, hardware
requirements and a quick build. Use [support](../SUPPORT.md) for questions and
bug reports, [contributing](../CONTRIBUTING.md) to work on changes, and the
[security policy](../SECURITY.md) to report vulnerabilities privately.

## Using and recovering the OS

- [Updates](updates.md): OS staging, Flatpak permissions and restart notifications.
- [Recovery](recovery.md): interrupted installation, root unlock, TPM resealing and home backups.
- [Boot health](boot-health.md): Greenboot checks, rollback and recovery notifications.
- [Chromium](chromium.md): browser configuration, updates, diagnostics and acceptance checks.

## Building and maintaining the project

- [Building](building.md): development and production images, prerequisites and overrides.
- [Architecture](architecture.md): source layout, installed files and application boundaries.
- [Testing](testing.md): unit, container, VM and interactive acceptance checks.
- [Releases](releases.md): signing, verification, publication, upgrade and rollback.
- [Branding and previews](../branding/README.md): visual assets and safe UI previews.
- [Plymouth](plymouth.md): boot splash, unlock prompts and graphical boot checks.
- [Licensing and third-party components](../THIRD_PARTY.md): source-license exceptions and packaged assets.

## Build measurements

[ISO build performance](build-performance.md) and [Chunkah layers](chunkah.md)
record dated development measurements and their validation scope. They are
historical results, not a current release certification. For a release's actual
coverage, inspect the matching workflow's reports and acceptance record as
explained in [testing](testing.md).
