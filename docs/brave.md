# Brave Origin

MertensiaOS includes the native stable Brave Origin RPM. GNOME opens HTTP,
HTTPS, HTML and XHTML with the upstream `brave-origin.desktop` entry. Choose
another browser in **Settings → Default Apps**. Origin stores profiles under
`~/.config/BraveSoftware/Brave-Origin` inside the user's encrypted home.
MertensiaOS passes `--skip-origin-startup-dialog` through both packaged desktop
entries so the free Linux edition opens directly, including on a fresh profile.
The standalone build omits Brave's product analytics, usage ping and Web
Discovery Project.

## Policies

Root-owned JSON under `/etc/brave/policies/` configures the browser:

| Policy | Value | Enforcement |
| --- | --- | --- |
| `SitePerProcess` | `true` | Mandatory |
| `SafeBrowsingProtectionLevel` | `1` (standard) | Recommended |
| `BackgroundModeEnabled` | `false` | Recommended |
| `SearchSuggestEnabled` | `false` | Recommended |
| `NetworkPredictionOptions` | `2` (disabled) | Recommended |
| `SpellCheckServiceEnabled` | `false` | Recommended |

Open `brave://policy` to see effective policy values and errors. Brave's
managed indicator reflects the local OS policies. Open `brave://sandbox` to
check namespace and seccomp protections, and `brave://process-internals` to
check site isolation. Brave Shields and Brave's other browser features use
upstream defaults. For protected media, use Brave's
[opt-in Widevine download](https://support.brave.app/hc/en-us/articles/360023851591-How-do-I-view-DRM-protected-content).

## Updates and rollback

Brave Origin security fixes reach installed systems through signed bootc releases.
They take effect after a restart. Release builds refresh Brave Origin from upstream's
stable RPM repository even when Fedora's base digest is unchanged. The Brave
repository is configured only for the image build. See
[automatic updates](updates.md) and [releases](releases.md).

An OS rollback also rolls back Origin's executable while keeping the newer
browser profile in the home. Resume browsing on a current patched release.

## Source and redistribution

The image installs packages from [Brave's stable RPM repository](https://brave.com/origin/linux/).
The image build adds the startup flag to the packaged desktop entries.
The build pins the public release signing key with fingerprint
`DBF1A116C220B8C7164F98230686B78420038257`. A signing-key rotation requires
reviewing the new key before replacing the checked-in copy.

Run `rpm -q brave-origin` to find the installed version. Its corresponding
source is available from the matching `v<version>` tags in
[brave-browser](https://github.com/brave/brave-browser/tags) and
[brave-core](https://github.com/brave/brave-core/tags). The release notes
identify the corresponding [Chromium source](https://chromium.googlesource.com/chromium/src/+refs).
The RPM carries its license at `/opt/brave.com/brave-origin/LICENSE` and bundled
notices are available at `brave://credits`. A Brave employee
[recommended Origin as a distro default for Omarchy](https://x.com/iAnonymous3000/status/2094111455490855052).
Brave’s [browser terms](https://brave.com/terms-of-use/#brave-browser-license)
describe its executable license and preserve rights granted by the source licenses.

## Acceptance checks

The [installed-system ISO probe](testing.md) exercises the upstream launcher
as an unprivileged account. It checks effective policies, namespace and seccomp
sandboxing, site isolation, HTML rendering, and GNOME default handlers and user
overrides.

Before release, in a normal installed GNOME session:

1. Open a link from another app on a fresh profile and verify it opens
   directly. Check that **New Window** and **New Incognito Window** also open
   directly when Origin is closed. Check `brave://policy`, `brave://sandbox` and
   the window system in `brave://gpu`. Open a local HTML file, then test
   downloads and uploads.
2. Test password save and retrieval after logout/login, an extension, video
   calls, and PipeWire screen sharing with both approval and cancellation.
   Check ordinary H.264/AAC and opt-in Widevine playback separately.
3. Create distinct browser data in two homed accounts and verify it persists
   separately across a restart. Change the default browser for one account and
   verify the choice survives login and an update.
4. With standard protection enabled, open **Database Manager** in
   `brave://safe-browsing`. After the first scheduled refresh, reload the page
   and verify that the database has populated. Open the phishing and
   malware test pages linked by
   [Brave's Safe Browsing guide](https://support.brave.app/hc/en-us/articles/15222663599629-Safe-Browsing-in-Brave).
   Both must display an interstitial. Record the RPM version and results
   before promoting the release.
