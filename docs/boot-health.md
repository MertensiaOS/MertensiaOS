# Boot health and automatic recovery

MertensiaOS installs Fedora's Rust Greenboot package and enables automatic
recovery on systems installed through the supported encrypted-disk installer.
Automatic OS updates stage with bootc; users choose when to restart. Greenboot
does not download updates or roll back user data. After automatic recovery, OS
staging pauses until an administrator acknowledges the recovery. See
[automatic updates](updates.md).

## What a healthy boot means

The desktop health check runs when the system starts `graphical.target`. It
allows up to 120 seconds for local desktop readiness, with a 150-second systemd
service timeout. The check requires:

- An accessible LUKS mapping backing the installed root, a writable `/var`, and
  an accessible `/boot` and GRUB environment.
- Active GDM, systemd-homed and AccountsService services, and responsive homed
  and AccountsService D-Bus methods.
- Successful completion of MertensiaOS's login-user synchronization. This
  oneshot service normally becomes inactive after succeeding.

No users need to exist yet. First-boot setup may be unfinished, and encrypted
homes may remain locked. Network connectivity, DNS, registry access, application
updates and a logged-in GNOME session are not health criteria. The upstream
`greenboot-default-health-checks` package is deliberately omitted.

The live installer and the unencrypted direct development VM image mask recovery
services and omit the notification autostart entry. They cannot establish that
recovery works on an installed, encrypted system.

## Retry and rollback behavior

Greenboot owns the GRUB retry counter and boot-success flags. MertensiaOS sets
`GREENBOOT_MAX_BOOT_ATTEMPTS=3` in `/etc/greenboot/greenboot.conf`, retaining the
packaged implementation's retry behavior. This is a retry budget, not a promise
that the initial failed boot counts as one of three total boots. Fedora's
competing boot-success service and user timer are masked.

On a failed health check, Greenboot retries and, when the budget is exhausted
and a rollback trigger exists, requests `bootc rollback`. A successful boot
clears the retry counter. The installed GRUB fragment also permits fallback
when GRUB cannot load an updated boot entry. Greenboot makes a detected fallback
permanent. The automatic recovery tests exercise both paths.

A fresh installation has no previous deployment to restore. A failed fallback
can also require manual intervention. These cases must stop after bounded
retries; the release tests explicitly check them. Firmware failures, an
unavailable encrypted root or a hung kernel can prevent Greenboot from running.
A hang may require a reset before another attempt, and a kernel panic is not
equivalent to GRUB failing to load a boot entry. Recovery is not a watchdog or a
guarantee against every pre-userspace failure.

Rollback preserves `/var` and encrypted homes. Keep separate data backups. The
root recovery key and TPM recovery procedures remain described in
[recovery](recovery.md).

## Recovery notifications

After a successful automatic fallback, a separate service records a recovery
event in `/var/lib/mertensia/boot-health/recovery.json`. The root-owned file is
readable by desktop users and contains only the event and boot IDs, UTC time and
restored image digest. Event collection never controls boot success.

At login, each ordinary GNOME user receives:

> MertensiaOS restored the previous OS version because an update could not start correctly.

The recovery notification also explains that automatic OS updates pause until
an administrator runs `sudo mertensia-update resume`. The notifier also watches
for events recorded after login. It remembers the last
delivered event in `$XDG_STATE_HOME/mertensia/boot-health-notification.json`
(normally `~/.local/state/mertensia/boot-health-notification.json`). Each user
sees an event once. An unavailable notification service leaves the event pending
for retry. The setup account is excluded; normal boots, healthy updates and
manual rollbacks do not create notifications. The latest recovery event remains
available to users who have not yet logged in.

## Inspect and configure

```sh
systemctl status greenboot-healthcheck.service greenboot-success.target
journalctl -b -u greenboot-healthcheck.service
journalctl -b -1 -u greenboot-healthcheck.service
sudo grub2-editenv /boot/grub2/grubenv list
bootc status
cat /var/lib/mertensia/boot-health/recovery.json
journalctl -b -u mertensia-boot-health-status.service
```

The event file does not exist until the first confirmed automatic recovery.
Greenboot also writes `/etc/motd.d/boot-status`. Persistent journaling is enabled
with a 128 MiB journal limit so previous-boot rollback diagnostics survive.
Health-check output reports local service states without home records, account
passwords or recovery keys.

Administrators can change the retry budget in `/etc/greenboot/greenboot.conf`.
To exclude the MertensiaOS desktop check temporarily, add its exact basename:

```ini
DISABLED_HEALTHCHECKS=("50-mertensia-desktop.sh")
```

This removes the desktop check from the acceptance criteria, so use it only
while diagnosing a failed system. Remove that entry to restore the check.
Greenboot also supports administrator checks in `/etc/greenboot/check/required.d`
and non-blocking checks in `/etc/greenboot/check/wanted.d`. Changes take effect
on the next boot; the packaged health service refuses manual restarts.

If recovery fails, select a working deployment in GRUB, inspect the journal and
use `sudo bootc rollback` followed by a reboot as appropriate. If no deployment
can unlock root or reach userspace, use the live installer and the documented
root recovery procedures. Changing TPM or Secure Boot state may require the
saved root recovery key regardless of Greenboot.
