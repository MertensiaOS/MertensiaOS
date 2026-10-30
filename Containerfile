FROM quay.io/fedora/fedora-bootc:45 AS accountsservice-build
RUN dnf -y install dnf5-plugins rpm-build
COPY system/patches/accountsservice-homed-enumeration.patch /build/accountsservice-homed-enumeration.patch
COPY scripts/build-accountsservice.sh /build/build-accountsservice.sh
RUN bash /build/build-accountsservice.sh

FROM quay.io/fedora/fedora-bootc:45

## Core / misc packages
RUN dnf -y install \
    openssl \
    fwupd \
    google-noto-fonts-all \
    glibc-all-langpacks \
    iso-codes \
    xkeyboard-config \
    kbd \
    systemd-cryptsetup \
    systemd-pam \
    authselect \
    cryptsetup \
    tpm2-tools \
    greenboot \
    python3-gobject \
    python3-dbus \
    gtk4 \
    libadwaita \
    polkit \
    shadow-utils \
    dracut \
    plymouth \
    plymouth-scripts \
    plymouth-graphics-libs \
    plymouth-plugin-two-step \
    plymouth-plugin-label \
    plymouth-theme-spinner \
    && dnf clean all

## GNOME
COPY --from=accountsservice-build /out/ /tmp/mertensia-accountsservice/
RUN dnf -y install \
    gdm \
    /tmp/mertensia-accountsservice/*.rpm \
    gnome-shell \
    gnome-session \
    gnome-control-center \
    nautilus \
    NetworkManager \
    xdg-desktop-portal \
    xdg-desktop-portal-gnome \
    && rm -rf /tmp/mertensia-accountsservice && dnf clean all

## set up systemd-homed
RUN authselect select local with-systemd-homed --force --nobackup && \
    authselect check && \
    install -Dm0600 /var/lib/authselect/checksum /usr/share/mertensia/authselect/checksum && \
    rm -f /var/lib/authselect/checksum && \
    systemctl enable systemd-homed.service

RUN systemctl enable NetworkManager.service && \
    systemctl enable gdm.service && \
    systemctl set-default graphical.target

## GNOME apps
RUN dnf -y install \
    chromium \
    flatpak \
    gnome-software \
    ptyxis \
    gnome-text-editor \
    gnome-firmware \
    && dnf clean all

## Native browser: retain Fedora's launcher and sandbox, with system defaults.
COPY system/config/chromium/policies /etc/chromium/policies
COPY system/config/chromium/mertensia-wayland.conf /etc/chromium/mertensia-wayland.conf
COPY system/config/desktop/gnome-mimeapps.list /usr/share/applications/gnome-mimeapps.list
RUN chmod 0755 /etc/chromium/policies /etc/chromium/policies/managed /etc/chromium/policies/recommended && \
    chmod 0644 /etc/chromium/policies/managed/mertensia.json /etc/chromium/policies/recommended/mertensia.json \
      /etc/chromium/mertensia-wayland.conf /usr/share/applications/gnome-mimeapps.list && \
    printf '\n# MertensiaOS native Wayland default\n. /etc/chromium/mertensia-wayland.conf\n' >> /etc/chromium/chromium.conf

## GNOME Software owns Flatpak updates and native permission review.
COPY system/config/flatpak/flathub.flatpakrepo /usr/share/flatpak/remotes.d/flathub.flatpakrepo
COPY system/config/gnome/50-mertensia-software.gschema.override /usr/share/glib-2.0/schemas/zz-mertensia-software.gschema.override
COPY system/config/systemd/user/gnome-software.service.d /usr/lib/systemd/user/gnome-software.service.d
RUN find /usr/lib64/gnome-software -type f \
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

COPY system/config/tmpfiles.d/mertensia.conf /usr/lib/tmpfiles.d/mertensia.conf

## Application modules; installer-only code is added in the live image.
COPY src/mertensia/__init__.py /usr/lib/mertensia/python/mertensia/__init__.py
COPY src/mertensia/accounts /usr/lib/mertensia/python/mertensia/accounts
COPY src/mertensia/firstboot /usr/lib/mertensia/python/mertensia/firstboot
COPY src/mertensia/ui /usr/lib/mertensia/python/mertensia/ui
COPY src/mertensia/boot_health /usr/lib/mertensia/python/mertensia/boot_health
COPY src/mertensia/updates /usr/lib/mertensia/python/mertensia/updates

## Reuse the packaged bootc scheduler, but never request an automatic reboot.
COPY system/bin/mertensia-update /usr/bin/mertensia-update
COPY system/config/systemd/bootc-fetch-apply-updates.service.d /usr/lib/systemd/system/bootc-fetch-apply-updates.service.d
COPY system/config/systemd/bootc-fetch-apply-updates.timer.d /usr/lib/systemd/system/bootc-fetch-apply-updates.timer.d
COPY system/config/systemd/mertensia-update-status.service /usr/lib/systemd/system/mertensia-update-status.service
COPY system/config/tmpfiles.d/mertensia-updates.conf /usr/lib/tmpfiles.d/mertensia-updates.conf
RUN chmod 0755 /usr/bin/mertensia-update && \
    systemctl enable bootc-fetch-apply-updates.timer mertensia-update-status.service

## Local desktop health and automatic bootc recovery, independent of networking.
COPY system/config/greenboot/greenboot.conf /etc/greenboot/greenboot.conf
COPY system/config/greenboot/50-mertensia-desktop.sh /usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh
COPY system/config/systemd/greenboot-healthcheck.service.d /usr/lib/systemd/system/greenboot-healthcheck.service.d
COPY system/config/systemd/greenboot-set-rollback-trigger.service.d /usr/lib/systemd/system/greenboot-set-rollback-trigger.service.d
COPY system/config/systemd/mertensia-boot-health-status.service /usr/lib/systemd/system/mertensia-boot-health-status.service
COPY system/config/journald/mertensia-boot-health.conf /usr/lib/systemd/journald.conf.d/50-mertensia-boot-health.conf
COPY system/config/tmpfiles.d/mertensia-boot-health.conf /usr/lib/tmpfiles.d/mertensia-boot-health.conf
COPY system/config/desktop/org.mertensia.BootHealth.desktop /etc/xdg/autostart/org.mertensia.BootHealth.desktop
COPY system/config/desktop/org.mertensia.BootHealth.desktop /usr/share/applications/org.mertensia.BootHealth.desktop
COPY system/bin/mertensia-boot-health /usr/libexec/mertensia-boot-health
COPY system/bin/mertensia-boot-health-notify /usr/libexec/mertensia-boot-health-notify
RUN chmod 0755 /usr/libexec/mertensia-boot-health /usr/libexec/mertensia-boot-health-notify \
      /usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh && \
    test -s /usr/lib/bootupd/grub2-static/configs.d/08_greenboot.cfg && \
    ! rpm -q greenboot-default-health-checks && \
    systemctl disable greenboot-healthcheck.service && \
    systemctl enable greenboot-healthcheck.service mertensia-boot-health-status.service && \
    systemctl --global mask grub-boot-success.timer grub-boot-success.service && \
    systemctl mask grub-boot-success.timer grub-boot-success.service

## Initial setup user with invalid (not blank) password
COPY system/config/gnome/mertensia-setup.json /usr/share/gnome-shell/modes/mertensia-setup.json
COPY system/config/gnome/setup-shell.conf /usr/share/mertensia/setup-shell.conf
COPY system/config/sysusers.d/mertensia-firstboot-sysusers.conf /usr/lib/sysusers.d/mertensia-firstboot.conf
RUN systemd-sysusers /usr/lib/sysusers.d/mertensia-firstboot.conf
COPY system/config/homed/homed.conf /etc/systemd/homed.conf
COPY system/config/gdm/custom.conf /etc/gdm/custom.conf
COPY system/config/desktop/mertensia-firstboot.desktop /etc/xdg/autostart/mertensia-firstboot.desktop
COPY system/config/desktop/mertensia-accounts.desktop /usr/share/applications/org.mertensia.Accounts.desktop
COPY system/config/polkit/org.mertensia.Accounts.policy /usr/share/polkit-1/actions/org.mertensia.Accounts.policy
COPY system/config/polkit/49-mertensia-accounts.rules /usr/share/polkit-1/rules.d/49-mertensia-accounts.rules
COPY system/bin/mertensia-setup-authorized /usr/libexec/mertensia-setup-authorized
COPY system/config/accountsservice/mertensia-setup /usr/share/mertensia/accountsservice-mertensia-setup
COPY system/config/tmpfiles.d/mertensia-firstboot-tmpfiles.conf /usr/lib/tmpfiles.d/mertensia-firstboot.conf
COPY system/config/systemd/mertensia-firstboot-retire.path /usr/lib/systemd/system/mertensia-firstboot-retire.path
COPY system/config/systemd/mertensia-firstboot-retire.service /usr/lib/systemd/system/mertensia-firstboot-retire.service
COPY system/config/systemd/mertensia-firstboot-finish.timer /usr/lib/systemd/system/mertensia-firstboot-finish.timer
COPY system/config/systemd/mertensia-firstboot-finish.service /usr/lib/systemd/system/mertensia-firstboot-finish.service
COPY system/bin/mertensia-firstboot /usr/bin/mertensia-firstboot
COPY branding /usr/share/mertensia/branding
COPY branding/fonts /usr/share/fonts/mertensia
## One native splash, inherited by the live installer and development images.
COPY branding/plymouth /usr/share/plymouth/themes/mertensia
COPY system/config/plymouth/plymouthd.conf /etc/plymouth/plymouthd.conf
COPY system/config/dracut/50-mertensia-plymouth.conf /usr/lib/dracut/dracut.conf.d/50-mertensia-plymouth.conf
COPY system/config/bootc/20-mertensia-plymouth.toml /usr/lib/bootc/install/20-mertensia-plymouth.toml
COPY system/bin/mertensia-check-plymouth-initrd /usr/libexec/mertensia-check-plymouth-initrd
RUN cp --update=none /usr/share/plymouth/themes/spinner/*.png /usr/share/plymouth/themes/mertensia/ && \
    fc-cache -f /usr/share/fonts/mertensia && \
    test "$(fc-match -f '%{file}' 'Figtree')" = /usr/share/fonts/mertensia/Figtree-Variable.ttf && \
    chmod 0755 /usr/libexec/mertensia-check-plymouth-initrd && \
    test "$(plymouth-set-default-theme)" = mertensia
COPY system/bin/mertensia-accounts-helper /usr/libexec/mertensia-accounts-helper
COPY system/bin/mertensia-sync-login-users /usr/libexec/mertensia-sync-login-users
COPY system/config/systemd/mertensia-login-users.service /usr/lib/systemd/system/mertensia-login-users.service
COPY system/config/systemd/mertensia-login-users.path /usr/lib/systemd/system/mertensia-login-users.path
RUN systemctl enable mertensia-login-users.service mertensia-login-users.path
COPY system/bin/mertensia-reseal-root /usr/bin/mertensia-reseal-root
RUN chmod 0755 \
      /usr/bin/mertensia-firstboot \
      /usr/bin/mertensia-reseal-root \
      /usr/libexec/mertensia-sync-login-users \
      /usr/libexec/mertensia-setup-authorized \
      /usr/libexec/mertensia-accounts-helper && \
    chmod 0600 /usr/share/mertensia/accountsservice-mertensia-setup && \
    systemctl enable mertensia-firstboot-retire.path

## Clean up temporary files
RUN dnf clean all && \
    rm -rf /run/dnf /run/tuned /run/selinux-policy /var/lib/dnf/repos && \
    rm -f /var/cache/ldconfig/aux-cache \
          /var/cache/ibus/bus/registry \
          /var/cache/swcatalog/cache/*.xb && \
    find /var/log -type f -delete

## MertensiaOS branding
COPY system/config/identity/os-release /usr/lib/os-release
COPY system/config/identity/system-release /usr/lib/mertensiaos-release
RUN ln -sfn ../usr/lib/mertensiaos-release /etc/system-release && \
    ln -sfn ../usr/lib/mertensiaos-release /etc/redhat-release && \
    ln -sfn ../usr/lib/mertensiaos-release /etc/fedora-release

## Trust the project's release key for installation and subsequent bootc updates.
COPY cosign.pub /etc/pki/containers/mertensia.pub
COPY system/config/containers/policy.json /etc/containers/policy.json
COPY system/config/containers/policy.json /usr/share/mertensia/bootc-policy.json
COPY system/config/containers/registries.yaml /etc/containers/registries.d/mertensia.yaml

## Keep the payload initramfs generic and include early LUKS/TPM discovery.
RUN kernel="$(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -n1)" && \
    test -n "$kernel" && \
    mkdir -p "$(realpath /root)" && \
    DRACUT_NO_XATTR=1 dracut --force --no-hostonly --zstd --add "crypt systemd systemd-cryptsetup tpm2-tss plymouth" \
      "/usr/lib/modules/${kernel}/initramfs.img" "$kernel" && \
    test -s "/usr/lib/modules/${kernel}/initramfs.img" && \
    /usr/libexec/mertensia-check-plymouth-initrd "/usr/lib/modules/${kernel}/initramfs.img"

RUN bootc container lint --fatal-warnings --no-truncate
