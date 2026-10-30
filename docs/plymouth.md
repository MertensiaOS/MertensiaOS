# Quiet Bloom boot splash

The installed system, development disk and live installer share the `mertensia`
Plymouth theme. Normal boot, restart and shutdown show a static pale bloom on ink
(`#21152F`) with Fedora's small spinner beneath it. There is no persistent text,
estimated percentage, progress bar, ending animation or intentional boot delay.
The bloom uses `soft-bloom-tint.svg`; its PNG canvas is 112 logical pixels wide.
Native watermark alignment places it near 42% of display height, with the spinner
centered at 58%. Plymouth lays out each display independently and handles scaling.

## Native unlock and messages

Automatic TPM unlock needs no additional UI. When the system asks for a root
recovery key, the native `two-step` plugin supplies the masked entry, actual
system prompt, keyboard-layout indicator and Caps Lock indicator. It returns to
the splash after successful unlock. Enter the saved root recovery key, rather
than an account password. This theme does not change encryption or recovery policy.

Prompts use Figtree through Pango, including in the initramfs. The graphical
console shown with Escape uses Noto Sans Mono, with regular and bold weights.
The initramfs includes its fontconfig alias rule as well as the fonts: without
the rule, Pango's generic `monospace` request can select proportional Noto Sans.
Native labels are white; the unmasked question-entry text is black and cannot be themed through
`two-step`. The entry therefore has a warm canvas interior, dark-surface frame,
and ink password bullets. The key icon derives from the shared symbolic master.
Native keyboard and Caps Lock assets remain Fedora's. Caps Lock state requires
an actual keyboard backend; the X11 preview does not establish its hardware state.
System messages appear at the top left so retry messages cannot overlap the
native prompt. Firmware/update/reset modes retain real progress and warnings;
normal boot never invents a percentage or reports update success.

## Assets and image integration

`branding/plymouth/` contains the native theme definition and generated PNGs.
Install `python3-cairo`, `python3-gobject` and `librsvg2`, then regenerate:

```sh
python3 scripts/generate-plymouth-assets.py
# Or render elsewhere to compare without replacing the committed assets:
python3 scripts/generate-plymouth-assets.py --output /tmp/plymouth-assets
```

The generator reads the brand palette and SVG masters without editing them.
Use the same Cairo/librsvg versions when comparing PNG bytes across machines.
The unit test regenerates and compares decoded pixels so renderer metadata or
compression changes do not masquerade as artwork changes.

The payload copies Fedora's packaged spinner and prompt PNGs into its own theme
directory without overwriting the project-owned images. Fedora assets retain
their package licenses. No preview renderer or X server ships in the payload.
The live installer inherits the directory, daemon configuration and dracut policy.
Both builds explicitly include the Plymouth module, Pango, font configuration
and Figtree. `mertensia-check-plymouth-initrd` rejects missing modules, renderers,
native entry assets, spinner frames, theme selection, font files or the console's
monospace alias rule (including its symlink target). Production image validation
repeats the check.

`/usr/lib/bootc/install/20-mertensia-plymouth.toml` supplies `quiet rhgb` at install
time. No `kargs.d` migration is shipped, so updates do not rewrite existing
installations' kernel arguments. Existing installations without `rhgb` must opt
into graphical boot using their normal bootc kernel-argument administration.
The production ISO already includes `quiet rhgb`.

## Native renderer captures

Build the payload first, then run as the owner of its Podman image store:

```sh
podman build -f Containerfile -t localhost/mertensiaos:base .
bash scripts/test-plymouth.sh localhost/mertensiaos:base /tmp/plymouth-preview
```

The capture directory must be new. The script builds a disposable preview layer
with `plymouth-devel` (Fedora's X11 renderer), Xvfb, Python Xlib and Pillow. It
runs with no network or host display/socket/device bindings. Only the capture
directory is writable from the host. It never invokes the host Plymouth daemon.

Each display directory contains `boot.png`, `recovery-empty.png`,
`recovery-masked.png`, `recovery-retry.png`, `question.png`, `returned-to-boot.png`,
`reboot.png`, `shutdown.png`, `firmware-upgrade.png` and renderer logs. Inputs are
synthetic and answers are discarded. `result.json` reports completed displays:
800×600, 1920×1080, 3840×2160 at forced 2× scale, and Plymouth's native mixed-head
800×600/640×480 fixture. Background and bloom checks reject silent fallback to
text. Review the captures for prompt/message overlap, readable bullets and
questions, clipping and appropriate spacing. CI retains the captures as an artifact.

These are real native renderer captures, but they do not prove a machine boots.

## Graphical VM acceptance

The regular ISO integration run continues to disable Plymouth, remove the splash
install defaults and use the serial console for reliable recovery diagnostics.
Run it separately from the graphical fixture:

```sh
sudo bash scripts/test-iso.sh --build --base-image localhost/mertensiaos:base \
  --work-dir /var/tmp/mertensia-serial
sudo bash scripts/test-iso.sh --build --graphical-boot \
  --base-image localhost/mertensiaos:base \
  --work-dir /var/tmp/mertensia-graphical
```

The graphical flag builds overlays using the production ISO arguments and
installed boot defaults. It retains all existing installation, TPM, Secure Boot,
GDM, account, and recovery checks. A private Unix QMP socket captures native boot
and the transition away from the splash; a fresh TPM forces the real recovery
prompt. Only after recognizing the bloom and entry does the harness type the
saved, case-sensitive recovery key through QMP. The key is never recorded in
the QMP requests' logs or report. Screenshots are PPM files named
`plymouth-live-*`, `plymouth-installed-*`, and `plymouth-recovery-*` in the new
work directory. A prebuilt ISO must have been built with the same flag.
The manual ISO smoke workflow exposes the flag as `graphical_boot` and retains
these screenshots with its diagnostics.

On hardware or a graphical VM (`--display gtk`), also check:

1. The ISO reaches the installer and an installed system reaches first setup or
   GDM without waiting for an animation. Restart and shutdown reuse the bloom
   without lingering after the system is ready to exit.
2. With fresh TPM state, an incorrect recovery key is rejected; the next prompt
   remains readable. The correct key unlocks root and continues boot. Verify
   keyboard-layout and Caps Lock indicators with the actual keyboard backend.
3. Escape switches between splash and console while a recovery prompt is open;
   keyboard entry remains usable. Repeat with graphics unavailable to exercise
   native text fallback. The serial integration run independently verifies real
   console recovery.
4. Review the splash, prompt and handoff on small, HiDPI, and mixed-resolution
   outputs. Confirm firmware progress displays the supplied percentage and
   warning, without representing background bootc downloads as offline updates.

Record the hardware/VM configuration and captures with release acceptance.
Preview success, an image build, and a dry run are not substitutes for these
boot checks.

## Troubleshooting

Press Escape to inspect console output. For one boot, remove `quiet rhgb` in
the boot menu and add `rd.plymouth=0 plymouth.enable=0` to expose console recovery.
The recovery key and existing TPM token continue to work independently of the
theme. Remove these temporary arguments on the next boot to restore the splash.

Inside a built image, `plymouth-set-default-theme` should print `mertensia` and
`fc-match -f '%{file}' Figtree` should select the bundled font. Inspect the kernel
initramfs with `mertensia-check-plymouth-initrd /usr/lib/modules/KERNEL/initramfs.img`.
Fix image sources and rebuild both image roles when changing the theme; changing
only the root filesystem's PNGs leaves an old splash embedded in the initramfs.
