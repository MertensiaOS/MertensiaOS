# Security policy

## Report a vulnerability privately

Use GitHub's [private vulnerability reporting form](https://github.com/MertensiaOS/MertensiaOS/security/advisories/new).
Private vulnerability reporting is enabled for this repository. Do not put
undisclosed exploit details, passwords, recovery keys or signing material in a
public issue, pull request or Discord channel.

Include:

- The affected image digest or source commit, and whether the latest available
  signed image or current source is also affected.
- The affected component and the privileges or physical access required.
- Reproduction steps using disposable accounts or disks, expected and actual
  behavior, and the likely impact.
- Relevant logs or a minimal proof of concept with secrets and personal data
  removed.

Maintainers will assess the report and coordinate remediation and disclosure
with the reporter. Use the private advisory conversation for follow-up details.

## Scope and updates

Reports about the installer, account helpers, encrypted homes, authorization,
boot/recovery behavior, update trust and build/release tooling are relevant.
For a bundled Fedora or other upstream component, identify that component and
version so maintainers can distinguish an upstream issue from an image
configuration or integration issue.

The project is under development and does not define a long-term support
schedule for older images. Security fixes reach installed systems through newly
published OS images; rebuilding source or merging a fix alone does not update an
installed machine. Chromium is part of the OS image, while Flatpak apps have a
separate update path. See [updates](docs/updates.md),
[browser updates](docs/chromium.md#updates-and-rollback) and
[release signature verification](docs/releases.md#verification).

Ordinary bugs and usage questions belong in the [support channels](SUPPORT.md).
