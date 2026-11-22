# Licensing and third-party components

The root [MIT license](LICENSE) covers MertensiaOS-authored code and documentation
unless a file states otherwise. It does not relicense upstream source, patches,
fonts or packages included in the OS image.

| Files or component | License and source |
| --- | --- |
| `system/gnome/cc-mertensia-accounts.c` and `.h` | GPL-3.0-or-later, as identified by their SPDX headers; [license text](LICENSES/GPL-3.0-or-later.txt). These are compiled into GNOME Settings. |
| `system/patches/gnome-control-center-mertensia-accounts.patch` | Changes to [GNOME Settings](https://gitlab.gnome.org/GNOME/gnome-control-center). Upstream files retain their GPL-2.0-or-later or GPL-3.0-or-later notices; [GPLv2](LICENSES/GPL-2.0-or-later.txt) and [GPLv3](LICENSES/GPL-3.0-or-later.txt) texts are included. |
| `system/patches/accountsservice-homed-enumeration.patch` | Changes to [AccountsService](https://gitlab.freedesktop.org/accountsservice/accountsservice), licensed GPL-3.0-or-later; [license text](LICENSES/GPL-3.0-or-later.txt). |
| `system/patches/skopeo-fulcio-build-signer.patch` | Changes to Fedora's Skopeo source and a backport from [containers/image](https://github.com/podman-container-tools/container-libs/pull/625), licensed Apache-2.0. [License text](LICENSES/Apache-2.0.txt). |
| `branding/fonts/Figtree-Variable.ttf` | Copyright 2022 The Figtree Project Authors; [SIL Open Font License 1.1](branding/fonts/OFL.txt). [Upstream Figtree](https://github.com/erikdkennedy/figtree). |
| Fedora packages and their assets | Retain each package's licenses and notices. In particular, the Plymouth spinner/prompt assets copied during image construction remain Fedora package assets; see [Plymouth integration](docs/plymouth.md#assets-and-image-integration). |
| Brave Origin stable RPM | The unmodified upstream package retains its license and third-party notices in `/opt/brave.com/brave-origin/LICENSE` and `brave://credits`. Brave's source is [MPL 2.0](https://www.mozilla.org/en-US/MPL/2.0/), with bundled components under their respective licenses. See [Brave Origin source and redistribution](docs/brave.md#source-and-redistribution). |

The build scripts rebuild Fedora source RPMs for AccountsService, GNOME
Settings and Skopeo with the checked-in patches and helper code. See the
[build guide](docs/building.md) for their integration. Other runtime packages
come from the repositories configured in the Containerfiles; their source and
licensing information is maintained by their upstream and Fedora projects.
Build tools such as Chunkah and the ISO image builder also retain their own
licenses.

For the exact packages and license expressions in a built image, run:

```sh
sudo podman run --rm localhost/mertensiaos:base rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\t%{LICENSE}\n'
```

Package license texts are installed under `/usr/share/licenses/` in the image.
This repository's root license describes the project source, not a single
license for the entire operating-system image. Preserve applicable notices when
redistributing source, fonts or images.
