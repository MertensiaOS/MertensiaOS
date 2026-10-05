# Installer branding

Quiet Bloom v1.0, from the supplied MertensiaOS brand identity kit (23 September 2026).
The SVG masters, variable Figtree font, SIL Open Font License, and `brand-tokens.json`
are copied unchanged from the kit. Soft Bloom B remains the kit's working master.

`installer.css` adapts the visual reference to GTK rather than using the kit's web
CSS. The installer reads light/dark semantic colors from `brand-tokens.json`, follows
the desktop theme, and retains native colors in high-contrast mode. Muted text and
border colors are additional UI roles. Status icons and explicit text carry meaning;
native destructive-action styling is retained for disk erasure.

The live-image Containerfile installs the font into the system font directory and
ships the artwork, tokens, and stylesheet under `/usr/share/mertensia-installer`.
Running the Python frontend directly uses this source directory's branding; the
font falls back to the system sans-serif unless Figtree is installed or configured
in fontconfig. Font license: `fonts/OFL.txt`.

The custom 24px symbolic icons use filled paths with approximately 2px outlines.
Keep strokes expanded to paths: GTK's symbolic recoloring can discard SVG stroke
attributes and fill their interiors instead. Check icons in GTK, not just an SVG
viewer, after editing them.

## Visual preview

Run `gtk4-broadwayd --address=127.0.0.1 --port=8087 :7`, open
`http://127.0.0.1:8087`, then run from the repository root:

```sh
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 GSETTINGS_BACKEND=memory \
  /usr/bin/python3 tests/preview_installer.py
```

The preview supplies sample disks and a sample recovery key and blocks subprocess
execution, installation, reboot, and key-file writes. Use `--page recovery` to
start on a particular screen, `--theme dark`, or `--width 800 --height 600` to
check a smaller display. `--capture /tmp/installer-preview` renders all eight
pages, including the bottom of any scrollable page, and exits. Keep the browser
viewport larger than the requested window when checking exact dimensions.

First-boot setup and the additional-account window reuse these tokens, icons and
fonts, with native Adwaita entry rows styled by `firstboot.css`. The system image
ships the assets under `/usr/share/mertensia/branding`.

Use `tests/preview_firstboot.py` with the same Broadway environment for a safe
account-setup preview. It replaces account creation with a sample completion
response and blocks subprocess execution. `--add-user` previews the account
window; `--theme dark --width 800 --height 600 --capture /tmp/firstboot-preview`
captures the device, account, error, working and completion states with scrolling.

The regional selectors show language/country names from `iso-codes`, native language
names from its translations, and keyboard descriptions from `xkeyboard-config` and
systemd's console-keymap mapping. Time zones use the installed zoneinfo country
tables. These packages are included in the system image; an unknown name falls back
to its identifier without hiding a supported option. The account helper always
receives the original system identifier.

The pickers support accent-insensitive search by name or identifier, keep the current
selection first, and show a checkmark. Down moves from search to the results; Enter
selects a single match or focuses the first of several matches. Escape clears search,
then closes without changing the selection. The local-time preview includes the
current UTC offset and refreshes every 30 seconds, using zoneinfo for daylight saving.
Keyboard and language changes take effect after setup.

To check the actual installed lists and exercise search, empty results, selection,
cancel, navigation persistence and retry behavior through GDK Broadway:

```sh
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 GSETTINGS_BACKEND=memory \
  /usr/bin/python3 tests/preview_firstboot.py --system-options --check-selectors \
  --capture /tmp/firstboot-regions
```

`--system-options` runs only the read-only listing commands before the preview blocks
subprocesses. Omit it for portable sample data. Captures also include all three
pickers and the no-results state. Repeat with `--theme dark --width 800 --height 600`
to check the smaller layout. The preview never creates an account or applies settings.
