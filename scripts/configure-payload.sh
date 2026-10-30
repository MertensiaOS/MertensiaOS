#!/usr/bin/env bash
# Configure the assembled payload filesystem in one image layer.
set -euo pipefail

# Each subshell preserves the failure boundary of the original RUN command.
(
  chmod 0755 /etc/chromium/policies /etc/chromium/policies/managed /etc/chromium/policies/recommended && \
      chmod 0644 /etc/chromium/policies/managed/mertensia.json /etc/chromium/policies/recommended/mertensia.json \
        /etc/chromium/mertensia-wayland.conf /usr/share/applications/gnome-mimeapps.list && \
      printf '\n# MertensiaOS native Wayland default\n. /etc/chromium/mertensia-wayland.conf\n' >> /etc/chromium/chromium.conf
)

(
  find /usr/lib64/gnome-software -type f \
        \( -name 'libgs_plugin_packagekit.so' -o -name 'libgs_plugin_dnf5.so' \
           -o -name 'libgs_plugin_rpm-ostree.so' -o -name 'libgs_plugin_fwupd.so' \
           -o -name 'libgs_plugin_epiphany.so' -o -name 'libgs_plugin_dkms.so' \) -delete && \
      rm -f /usr/share/applications/gnome-software-local-file-packagekit.desktop \
        /usr/share/applications/gnome-software-local-file-fwupd.desktop \
        /usr/share/metainfo/org.gnome.Software.Plugin.Fwupd.metainfo.xml \
        /usr/share/metainfo/org.gnome.Software.Plugin.Epiphany.metainfo.xml && \
      glib-compile-schemas --strict /usr/share/glib-2.0/schemas && \
      systemctl --global enable gnome-software.service && \
      test -f /usr/lib64/gnome-software/plugins-23/libgs_plugin_flatpak.so && \
      ! find /usr/lib64/gnome-software -name 'libgs_plugin_*.so' | grep -E '/libgs_plugin_(packagekit|dnf5|rpm-ostree|fwupd|epiphany|dkms)\.so$'
)

(
  chmod 0755 /usr/bin/mertensia-update && \
      systemctl enable bootc-fetch-apply-updates.timer mertensia-update-status.service
)

(
  chmod 0755 /usr/libexec/mertensia-boot-health /usr/libexec/mertensia-boot-health-notify \
        /usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh && \
      test -s /usr/lib/bootupd/grub2-static/configs.d/08_greenboot.cfg && \
      ! rpm -q greenboot-default-health-checks && \
      systemctl disable greenboot-healthcheck.service && \
      systemctl enable greenboot-healthcheck.service mertensia-boot-health-status.service && \
      systemctl --global mask grub-boot-success.timer grub-boot-success.service && \
      systemctl mask grub-boot-success.timer grub-boot-success.service
)

(
  systemd-sysusers /usr/lib/sysusers.d/mertensia-firstboot.conf
  systemctl --global enable mertensia-setup-awake.service
)

(
  # Source-installed icons do not trigger RPM's icon-cache update hooks.
  gtk-update-icon-cache --force /usr/share/icons/hicolor
)

(
  cp --update=none /usr/share/plymouth/themes/spinner/*.png /usr/share/plymouth/themes/mertensia/ && \
      fc-cache -f /usr/share/fonts/mertensia && \
      test "$(fc-match -f '%{file}' 'Figtree')" = /usr/share/fonts/mertensia/Figtree-Variable.ttf && \
      chmod 0755 /usr/libexec/mertensia-check-plymouth-initrd && \
      test "$(plymouth-set-default-theme)" = mertensia
)

(
  systemctl enable mertensia-login-users.service mertensia-login-users.path
)

(
  chmod 0755 \
        /usr/bin/mertensia-firstboot \
        /usr/bin/mertensia-reseal-root \
        /usr/libexec/mertensia-sync-login-users \
        /usr/libexec/mertensia-setup-authorized \
        /usr/libexec/mertensia-accounts-helper && \
      chmod 0600 /usr/share/mertensia/accountsservice-mertensia-setup && \
      systemctl enable mertensia-firstboot-retire.path
)

(
  dnf clean all && \
      rm -rf /run/dnf /run/tuned /run/selinux-policy /var/lib/dnf/repos && \
      rm -f /var/cache/ldconfig/aux-cache \
            /var/cache/ibus/bus/registry \
            /var/cache/swcatalog/cache/*.xb && \
      find /var/log -type f -delete
)

(
  ln -sfn ../usr/lib/mertensiaos-release /etc/system-release && \
      ln -sfn ../usr/lib/mertensiaos-release /etc/redhat-release && \
      ln -sfn ../usr/lib/mertensiaos-release /etc/fedora-release
)

(
  kernel="$(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -n1)" && \
      test -n "$kernel" && \
      mkdir -p "$(realpath /root)" && \
      DRACUT_NO_XATTR=1 dracut --force --no-hostonly --zstd --add "crypt systemd systemd-cryptsetup tpm2-tss plymouth" \
        "/usr/lib/modules/${kernel}/initramfs.img" "$kernel" && \
      test -s "/usr/lib/modules/${kernel}/initramfs.img" && \
      /usr/libexec/mertensia-check-plymouth-initrd "/usr/lib/modules/${kernel}/initramfs.img"
)
