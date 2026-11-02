# Contributing to MertensiaOS

MertensiaOS is a Fedora bootc desktop under active development. Contributions to
code, documentation, accessibility, translations and testing are welcome.

## Start here

Read the [README](README.md), [architecture](docs/architecture.md) and
[build guide](docs/building.md). Search existing
[issues](https://github.com/MertensiaOS/MertensiaOS/issues) and
[pull requests](https://github.com/MertensiaOS/MertensiaOS/pulls) before starting.
For substantial changes to account management, encryption, installation or
updates, describe the problem and proposed behavior in an issue first so the
design can be discussed before implementation.

Use [support channels](SUPPORT.md) for questions and the [security policy](SECURITY.md)
for vulnerabilities. Keep discussion respectful and focused on the work; personal
attacks, harassment and disclosure of someone else's private information are not
acceptable.

## Work on a change

Fork the repository, create a branch and keep the change focused on one problem.
Follow the existing code style and application boundaries. GTK UIs run without
root privileges; privileged helpers must independently authorize and validate
requests. Keep image configuration beside its system consumer, and update file
manifests and Containerfiles when adding installed files.

Include documentation for behavior changes and regression coverage where it
helps establish correctness. Preserve upstream copyright and license notices;
see [licensing and third-party components](THIRD_PARTY.md) for exceptions to the
root MIT license. Do not include build outputs, private signing material,
passwords or recovery keys.

## Commit messages and PR titles

Use [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)
for commit subjects and pull request titles:

```text
<type>[optional scope][!]: <description>
```

Use a lowercase type, an optional scope naming the affected component, and a
short description in the imperative, without a trailing period. Common types:

- `feat`: new user-facing functionality.
- `fix`: a bug fix.
- `docs`: documentation changes.
- `build` or `ci`: image/build tooling or CI workflows.
- `refactor`, `perf`, `test`, `style` or `chore`: restructuring, performance,
  tests, formatting or maintenance changes.
- `revert`: reverting an earlier change; identify the reverted commit in the body.

Examples:

```text
feat(accounts): redirect GNOME Users to Mertensia Accounts
fix(installer): reject disks mounted after selection
docs: clarify encrypted-home recovery
ci: run native keyring acceptance checks
```

Keep unrelated changes in separate commits where practical. Add a body when
needed to explain the reason, behavior or validation. Mark a breaking change
with `!` before the colon or a `BREAKING CHANGE:` footer, and explain its impact
and required migration. A squash-merge message should follow the same convention
and describe the final change being merged.

## Validate and submit

Install the Fedora test dependencies from the [README](README.md#run-checks), then
run from the repository root:

```sh
python3 scripts/check-source.py
python3 tests/run_tests.py
git diff --check
```

For documentation-only changes, check links, examples and any changed YAML or
JSON. For image, installer, authentication or update behavior, use the relevant
[container and VM tests](docs/testing.md). Use disposable virtual disks for
installation and recovery tests. [UI previews](branding/README.md) help review
visual changes without modifying real accounts or disks.

Open a pull request against `main`. Explain the problem, resulting behavior and
validation commands/results. Include screenshots for visible UI changes when
useful. State what was not exercised; a unit test, container check or preview
does not establish that a production ISO boots or that a graphical login works.
Maintainers may request changes before merging.
