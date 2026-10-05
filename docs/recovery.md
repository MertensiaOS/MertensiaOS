# Recovery and backups

## Two encryption boundaries

The installer creates a LUKS root volume with TPM unlock bound to Secure Boot
PCR 7, plus a high-entropy root recovery key. The first-boot account uses a
separate systemd-homed encrypted home unlocked with the user's password.
The root recovery key does not unlock user homes. Save the root recovery key
outside the computer and keep home passwords and backups separately.

## Interrupted installation

After an installer error, stay in the current live boot if you need the retained
keys. Root-only state is kept under `/run/mertensia-install-state` and lasts only
until reboot. The error report tells you whether an installed deployment can
be finalized or whether only key recovery is possible.

Use the same target's stable `/dev/disk/by-id/...` path shown in the installer:

```sh
pkexec /usr/libexec/mertensia-installer-helper resume --disk /dev/disk/by-id/REPLACE_WITH_TARGET
```

`resume` never repartitions. It checks the disk identity and LUKS UUID, verifies
unlock methods and shows the retained recovery key. Save it outside the live
system, then type `confirm-recovery` and Enter to finalize.

If deployment creation did not finish, recover the key first:

```sh
pkexec /usr/libexec/mertensia-installer-helper recover --disk /dev/disk/by-id/REPLACE_WITH_TARGET
```

Save the displayed key, then type `confirm-recovery` and Enter. This completes
key recovery and releases retained state; it does not claim a partial deployment
is bootable. A fresh installation will erase the selected disk again. Rebooting
before saving retained keys loses the live session's temporary key files.

Never delete retained state or change its permissions to bypass a recovery error.
The helper refuses another installation while a recoverable attempt is pending
and prevents concurrent helpers from operating on the shared target/mapping.

If formatting failed before an encrypted header was created, the error can
instead offer `discard-prepared`. This command independently verifies the disk,
partition and absence of an encrypted header before deleting unused key state:

```sh
pkexec /usr/libexec/mertensia-installer-helper discard-prepared --disk /dev/disk/by-id/REPLACE_WITH_TARGET
```

Use it only when offered by the helper. Any probe error, unreadable device or
existing header prevents discarding. The diagnostic explicitly identifies when
restarting an unused preparation attempt is safe.

## TPM or Secure Boot changes

If automatic root unlock fails after a firmware update, TPM reset or Secure Boot
change, enter the saved root recovery key at the boot prompt. Once the machine
boots with Secure Boot enabled and a usable TPM, run:

```sh
sudo mertensia-reseal-root
```

Enter the root recovery key when prompted. The tool verifies it, replaces the
TPM enrollment for the current Secure Boot state and retains the recovery slot.
Restart and verify automatic unlock before putting the saved key away.

## Homes and account passwords

There is currently no separate home recovery key enrolled by the account app.
Forgetting the home password can make the home inaccessible even to root.
Back up important files while the home is unlocked, store backups independently,
and test restoration. A root filesystem backup alone does not provide the
password needed to unlock a systemd-homed home.

First-boot account creation is retryable before its completion marker commits.
Once it commits, the setup identity loses authorization and the app moves to
login. If automatic login handoff fails, follow the completion screen's restart
instruction; do not try to create the same first account again.

For release and update rollback, see [releases](releases.md). Account maintenance
features beyond creation and a dedicated home-recovery UI remain future work.
