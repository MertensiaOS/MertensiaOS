# Chromium

MertensiaOS includes Fedora's native `chromium` RPM as its default web browser.
Fedora owns the executable, launcher, desktop entry and sandbox components.
MertensiaOS supplies local policies and appends a configuration hook to
`/etc/chromium/chromium.conf`: when `WAYLAND_DISPLAY` is present, the launcher
adds `--ozone-platform=wayland`. Non-Wayland sessions retain Fedora's backend
selection. Fedora's `CHROMIUM_USER_FLAGS` override remains available.

System GNOME MIME defaults select `chromium-browser.desktop` for HTTP, HTTPS,
HTML and XHTML. Existing per-user choices take precedence. Change the browser
in **Settings → Default Apps**; login never rewrites user MIME settings.
Profiles use Chromium's normal location under the user's home (normally
`~/.config/chromium`), inside the encrypted homed volume. Profiles and data from
other browser packages, including Flatpaks, are not automatically migrated.

## Policies

Root-owned files in `/etc/chromium/policies/managed/mertensia.json` and
`/etc/chromium/policies/recommended/mertensia.json` supply these defaults:

| Policy | Value | Enforcement |
| --- | --- | --- |
| `SitePerProcess` | `true` | Mandatory |
| `MetricsReportingEnabled` | `false` | Mandatory |
| `UrlKeyedAnonymizedDataCollectionEnabled` | `false` | Mandatory |
| `SafeBrowsingProtectionLevel` | `1` (standard) | Recommended |
| `BackgroundModeEnabled` | `false` | Recommended |
| `SearchSuggestEnabled` | `false` | Recommended |
| `NetworkPredictionOptions` | `2` (disabled) | Recommended |
| `SpellCheckServiceEnabled` | `false` | Recommended |

Mandatory policies cannot be changed in browser preferences. Recommended
policies are defaults that users can change, including choosing enhanced Safe
Browsing. Local spellchecking, JavaScript/JIT, extensions, password saving,
video calls and ordinary website permission prompts remain available. Search
provider selection is unchanged. These settings reduce optional reporting;
they do not prevent all browser network requests or provide anonymity.

Site isolation is already enabled upstream; the mandatory policy prevents
opting out. Native namespace and seccomp sandboxing remain enabled. No external
sandbox wrapper or disabling flags are added. This is configuration hardening
of Fedora Chromium, not Trivalent's patched browser or secureblue's OS hardening.

Open `chrome://policy` to see policy values, enforcement levels and errors.
Chromium's “managed by your organization” indicator reflects these local OS
policies; it does not mean MertensiaOS enrolled the browser in cloud management.
Open `chrome://sandbox` to check namespace and seccomp protections, and
`chrome://process-internals` to inspect site isolation. In a GNOME Wayland
session, `chrome://gpu` should identify the Ozone platform as Wayland.
The upstream [Linux policy guide](https://www.chromium.org/administrators/linux-quick-start/)
explains managed and recommended policy files. Avoid defining the same key in
multiple files at the same level: filename ordering does not resolve conflicts.
Administrator edits in `/etc` can be retained by bootc's configuration merging;
validate effective settings after upgrades rather than assuming defaults won.

## Updates and rollback

GNOME Software updates Flatpak apps, not this Chromium installation. Browser
fixes require a maintainer to publish a fresh signed OS image, the installed
system to stage it, and the user to restart into it. Release builds use
`--pull --no-cache` to refresh RPM installations even when the base image digest
is unchanged. Automated OS release publication is deferred; the daily bootc
timer cannot obtain fixes that maintainers have not published. See
[automatic updates](updates.md) and [releases](releases.md).

OS rollback may restore an older, vulnerable Chromium while leaving the newer
profile data in the home directory. Chromium does not guarantee profile
downgrade compatibility. Back up important browser data, avoid using an older
browser for routine browsing, and return to a patched release promptly. Never
remove profile downgrade protections to make a rollback appear healthy.

## Acceptance checks

The [ISO integration suite](testing.md) launches a headless browser as a
disposable unprivileged guest user. It checks the actual browser's policy
export for correct values, machine scope, source and enforcement, rejecting
unknown, ignored, conflicting or erroneous policies. It verifies namespace,
PID/network namespace and seccomp protections and the active site isolation
mode. Isolated XDG directories exercise all four default handlers and verify
that explicit user choices take precedence. The diagnostic connection uses
inherited private pipes and no listening TCP port. Production images contain
none of this test tooling.

Headless checks do not establish graphical integration or working Safe Browsing
network services. Before release, in a normal installed GNOME session:

1. Verify `chrome://policy`, `chrome://sandbox` and Wayland in `chrome://gpu`.
   Test links from another app, HTML/XHTML files, downloads and uploads.
2. Test password save/retrieval after logout/login, an extension, local
   spellchecking, camera/microphone permission prompts, and PipeWire screen
   sharing with both approval and cancellation.
3. Log into two homed accounts. Create distinct browser data, restart the OS,
   and verify each profile persists independently. Select another browser in
   one account and verify its choice survives login and an OS update.
4. With networking enabled and standard protection selected, enable internal
   debugging pages at `chrome://chrome-urls` if needed, then open
   `chrome://safe-browsing` and verify list/database updates succeed. Allow a
   fresh profile several minutes to fetch its initial protection lists. Visit the
   harmless phishing and malware test links at
   [Google's Safe Browsing test site](https://testsafebrowsing.appspot.com/).
   Both must produce the browser's security interstitial. Record the Chromium
   RPM version and test results. A loaded policy or ordinary HTTPS access does
   not prove Safe Browsing's API credentials and protection work. Failure is a
   release blocker; do not disable the protection or mark it verified.

Extra codecs/DRM sources, browser forks, global namespace/kernel/SELinux changes
and automatic release scheduling are outside this integration.
