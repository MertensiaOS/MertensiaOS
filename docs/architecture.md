# Codebase architecture

MertensiaOS builds an installed bootc desktop payload, then derives a disposable
live installer from it. Application source, image configuration and developer
tooling have separate homes:

```text
src/mertensia/       Importable Python application packages
  accounts/         Privileged account helper and login-user synchronisation
  firstboot/        Setup/account UI, regional metadata and picker widget
  installer/        Live installer UI, disk checks, deployment and recovery
  ui/               Shared branding paths and GTK theme loading
branding/           Shared stylesheets, tokens, artwork, icons and licensed font
system/
  bin/              Installed desktop launchers and shell utilities
  config/           Installed desktop configuration, grouped by consumer
  patches/          Downstream changes to upstream packages
installer/
  bin/              Live installer launchers
  config/           Live session, authorisation and ISO configuration
scripts/            Build, release, source validation and VM entry points
  previews/         Safe GTK previews using sample data
tests/             Test runner and test suites
  unit/             Application, configuration, packaging and script checks
  integration/      Disposable QEMU harness, guest agent and image overlays
docs/               Build, test, release, recovery and architecture guides
```

`output/` contains generated disks and ISOs. `.build/` is local build state.
Both are ignored by Git and excluded from the Podman context along with caches
and private signing material in `.containerignore`.

## Images and installed files

The root `Containerfile` builds the production-capable desktop payload. It installs
accounts, firstboot and shared UI modules under
`/usr/lib/mertensia/python/mertensia/`, and branding under
`/usr/share/mertensia/branding`. Configuration source directories follow their
consumers (`systemd`, `polkit`, `gdm`, `homed`, `containers`, `desktop`, `gnome`,
`sysusers.d`, `tmpfiles.d`, `identity` and `accountsservice`). The Containerfile
maps these files to the paths required by each system component.

`Containerfile.installer` derives from that payload, adds only the live installer
package and launchers, and overrides live-session configuration. It inherits
shared branding from the payload. Installer code is absent from the clean payload.
`Containerfile.dev` adds the disposable testing account for direct VM images.
The integration Containerfile adds a test-only agent and service to disposable
payload/live overlays; release validation rejects these artifacts in production.

Executable Python launchers remain at the paths referenced by desktop entries,
polkit and systemd. They use Python isolated mode (`-I`) and prepend only the
absolute, image-owned module directory. Working-directory modules and
`PYTHONPATH` cannot override privileged helper imports. Source previews and the
test runner explicitly add the checkout's `src/` directory; application packages
do not modify the import path themselves.

## Application boundaries

The GTK UIs launch their respective privileged helpers through `pkexec`.
UI code does not import privileged helper modules. Helpers validate requests and
system state independently of client-side validation.

The account helper's `backend.py` identifies the caller and controls account
lifecycle transitions. `validation.py` checks untrusted requests, `state.py`
provides durable writes and the exclusive lifecycle lock, and `operations.py`
implements D-Bus and system command operations. Firstboot and the additional
account window share the same UI; regional metadata stays in GTK-free
`regions.py`, while the search dialog lives in `picker.py`.

The installer helper's `backend.py` orchestrates installation and its CLI.
`devices.py` handles hardware discovery, selected-disk identity and busy-device
checks; `deployment.py` handles image configuration and installed-system setup;
`recovery.py` owns private retained state, locking, resume/discard and encryption
key confirmation. `commands.py` owns subprocess errors and JSON events;
`constants.py` holds installer paths and policy constants.

Installer helpers emit newline-delimited JSON events. Installation retains its
temporary LUKS key until the UI sends `confirm-recovery` on stdin and unlock/key
removal checks succeed. The account helper accepts one JSON request on stdin and
returns a JSON result; caller identity and persisted lifecycle state determine
which operation is permitted. Secrets must stay out of argv and diagnostics.

## Working on the repository

Run `python3 scripts/check-source.py` and `python3 tests/run_tests.py` from the
repository root. The runner discovers both `tests/unit/` and the host-side
integration harness checks and fails if required tests are skipped. For a focused
suite from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_accounts_backend.py'
```

Packaging tests stage the Containerfile COPY inputs into a temporary image tree,
exercise relocated launchers with an untrusted working directory and PYTHONPATH,
and verify required configuration destinations. They do not install the OS or
replace the [VM and interactive acceptance tests](testing.md).

When adding an application module, keep its package included by the appropriate
Containerfile. Put executable entry points in the relevant `bin/`, shared artwork
in `branding/`, and configuration beside its system consumer. Update Containerfile
inputs, tests and documentation together. ISO CI watches source, branding,
configuration, scripts, tests and build-context rules.

For visual checks, use the safe [preview tools](../branding/README.md).
For build and release commands, see [building](building.md) and
[releases](releases.md). Keep test overlays separate from releasable images.
