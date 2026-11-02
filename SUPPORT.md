# Getting help

MertensiaOS is under active development. Start with the [README](README.md) for
hardware requirements and the [documentation index](docs/README.md) for setup,
updates, recovery and development.

- Ask usage and development questions in [Discord](https://discord.gg/BXJByrmMr).
- Report reproducible bugs and propose features through
  [GitHub issues](https://github.com/MertensiaOS/MertensiaOS/issues).
- Report vulnerabilities privately using the [security policy](SECURITY.md).

For a bug report, include the exact source commit or installed image digest,
how the image was built or obtained, reproduction steps, expected and actual
behavior, and relevant logs. For boot or installation problems, include the
hardware or VM configuration, RAM, and Secure Boot/TPM state. `bootc status` can
identify an installed deployment; `git rev-parse HEAD` identifies a checkout.

Remove passwords, root recovery keys, home recovery material, signing keys,
access tokens, and private machine or account details before sharing logs or
screenshots. Do not attach disk images containing personal files or real user
homes. Diagnostic commands and recovery procedures are in the linked guides;
use [recovery documentation](docs/recovery.md) before attempting installation
again on a disk containing data you need.

Public issue reports and Discord conversations are not private support channels.
