#!/usr/bin/env bash
# Configure the assembled installer filesystem in one image layer.
set -euo pipefail

# Each subshell preserves the failure boundary of the original RUN command.
(
  rm -f \
      /etc/xdg/autostart/org.mertensia.BootHealth.desktop \
      /etc/xdg/autostart/mertensia-firstboot.desktop \
      /usr/share/applications/org.mertensia.Accounts.desktop \
      /usr/share/applications/org.gnome.Tour.desktop \
      /usr/share/dbus-1/services/org.gnome.Tour.service
)

python3 /run/mertensia-build/clean-desktop.py
python3 /run/mertensia-build/check-desktop-policy.py

(
  systemctl mask greenboot-healthcheck.service greenboot-set-rollback-trigger.service \
        greenboot-success.target mertensia-boot-health-status.service \
        bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service mertensia-update-status.service && \
      systemctl --global mask gnome-software.service
)

(
  systemd-sysusers /usr/lib/sysusers.d/mertensia-installer.conf
)

(
  glib-compile-schemas --strict /usr/share/glib-2.0/schemas
)

(
  fc-cache -f /usr/share/fonts/mertensia
)

(
  mkdir -p \
        /usr/lib/mertensia-installer \
        /usr/lib/bootc-image-builder && \
      sed -e "s|@SOURCE_IMAGE@|${SOURCE_IMAGE}|g" \
          -e "s|@TARGET_IMAGE@|${TARGET_IMAGE}|g" \
          -e "s|@BUILD_MODE@|${BUILD_MODE}|g" \
          /tmp/install.conf.in > /usr/lib/mertensia-installer/install.conf && \
      rm -f /tmp/install.conf.in && \
      ln -sfn ../image-builder/bootc/iso.yaml /usr/lib/bootc-image-builder/iso.yaml && \
      chmod 0755 /usr/bin/mertensia-installer /usr/libexec/mertensia-installer-helper && \
      dnf clean all && \
      rm -rf /run/dnf /var/lib/dnf/repos && \
      rm -f /var/cache/ldconfig/aux-cache && \
      find /var/log -type f -delete
)

(
  kernel="$(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -n1)" && \
      test -n "$kernel" && \
      DRACUT_NO_XATTR=1 dracut --force --no-hostonly --zstd \
        --add "dmsquash-live dmsquash-live-autooverlay crypt systemd systemd-cryptsetup tpm2-tss plymouth" \
        "/usr/lib/modules/${kernel}/initramfs.img" "$kernel" && \
      test -s "/usr/lib/modules/${kernel}/initramfs.img" && \
      /usr/libexec/mertensia-check-plymouth-initrd "/usr/lib/modules/${kernel}/initramfs.img"
)

(
  mkdir -p /boot/efi && \
      cp -a /usr/lib/efi/*/*/EFI /boot/efi/ && \
      test -e /boot/efi/EFI/fedora/shimx64.efi && \
      test -e /boot/efi/EFI/fedora/gcdx64.efi
)
