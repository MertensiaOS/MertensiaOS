# Automatic updates

MertensiaOS uses Fedora's stock GNOME Software for Flatpak apps and bootc for the
OS. They have separate update controls. Firmware stays manual in GNOME Firmware.

The preinstalled native Chromium browser is part of the OS image. Its security
fixes require a maintainer-published release and a restart into that release;
Software does not update it. See [Chromium](chromium.md).

## Application updates and permissions

Open **Software** to discover, install and update apps from Flathub. Background
updates are enabled by default for ordinary GNOME sessions. Software uses its
native daily checking, network and power policy; downloads may be deferred.
It does not run in the setup session or the live installer. User installations
become accessible when their encrypted home is unlocked at login.

Updates with unchanged or reduced permissions can install automatically. An
update requesting additional permissions remains pending and is marked as
requiring additional permissions. Select the app's update details to see the
new access before manually updating it. Software uses its upstream permission
comparison and descriptions; MertensiaOS adds no permission parser or updater.

Stock Software does not require opening these details before a manual update.
**Update All** can approve pending permission changes, so inspect flagged apps
first. This is the chosen native review flow, not a mandatory approval dialog.
Portal prompts and local permission overrides remain separate Flatpak features.

Use Software's update preferences to turn automatic application updates and
notifications on or off. Software manages apps only; its status does not report
whether the OS image is current. Running applications use their new version
when next started; automatic updates do not close applications.

## OS staging and restart

The packaged `bootc-fetch-apply-updates.timer` checks daily, catches up missed
runs after boot, and adds up to one hour of random delay. Its service is
overridden to run `mertensia-update stage`, which calls `bootc upgrade` without
`--apply`. Updates activate on the next normal restart. Each user receives one
notification per staged digest: “An OS update is ready. Restart when convenient.”

Staging requires successful Greenboot checks and completed recovery collection.
It defers on offline, metered or unknown connections, and accepts NetworkManager's
explicit or inferred unmetered state. It preserves existing staged deployments,
queued manual rollbacks and digest pins. Local development references and
references without container signature policy enforcement are skipped.
Live installers and direct development VM images mask the updater.

The installed signed channel and container policy remain authoritative. Signature
rejection, download failure and insufficient storage leave the running OS in
place and do not affect boot-health checks. Failures retry on the next scheduled
run; after fixing connectivity or storage, an administrator can trigger an
earlier attempt. Automatic staging does not request a restart, although
Greenboot can still restart to recover from a failed boot.

```sh
mertensia-update status
sudo bootc status
systemctl status bootc-fetch-apply-updates.timer
journalctl -u bootc-fetch-apply-updates.service
sudo systemctl start bootc-fetch-apply-updates.service
```

`status` prints JSON. For desktop users it reads the public status file; root
refreshes that file from bootc. The record contains its boot ID, staged digest,
pending recovery event ID and the last deferral/failure reason. State lives in
`/var/lib/mertensia/updates/`; only root can write it. Notifications ignore
records from older boots and retry if the notification service is unavailable.
The publisher runs after boot health collection and whenever bootc activates
`bootc-status-updated.target`. Manual bootc upgrades, rollbacks and removal of
staged deployments therefore refresh the desktop record without waiting for
the automatic update timer. The publisher only reads status; it does not stage
an update or request a restart.

To disable or enable automatic OS staging:

```sh
sudo systemctl disable --now bootc-fetch-apply-updates.timer
sudo systemctl enable --now bootc-fetch-apply-updates.timer
```

Disabling a timer does not cancel an already running download or discard an
already staged deployment. Inspect bootc status before restarting.

## After recovery or manual rollback

A confirmed automatic recovery pauses OS staging until an administrator
acknowledges that recovery event. This prevents downloading the failing image
again. App updates continue independently. After inspecting and addressing the
failure:

```sh
sudo mertensia-update resume
```

This records an acknowledgement; it neither downloads an update nor restarts.
The next scheduled attempt can stage the channel's current image. A later
recovery event pauses staging again. Timer enablement is a separate control.

Before a manual rollback, disable the timer and wait for any running staging
service to finish. Then use `sudo bootc rollback` and restart. Re-enable the
timer when ready to follow the channel again. See [boot health](boot-health.md)
and [recovery](recovery.md) for diagnosis and encryption recovery.
