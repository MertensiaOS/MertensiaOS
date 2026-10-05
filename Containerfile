FROM quay.io/fedora/fedora-bootc:45 AS accountsservice-build
RUN dnf -y install dnf-plugins-core rpm-build
COPY system/accountsservice-homed-enumeration.patch /build/accountsservice-homed-enumeration.patch
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
    python3-gobject \
    python3-dbus \
    gtk4 \
    libadwaita \
    polkit \
    shadow-utils \
    dracut \
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
    ptyxis \
    gnome-text-editor \
    gnome-firmware \
    && dnf clean all

COPY tmpfiles.d/mertensia.conf /usr/lib/tmpfiles.d/mertensia.conf

## Initial setup user with invalid (not blank) password
COPY system/mertensia-setup-session.json /usr/share/gnome-shell/modes/mertensia-setup.json
COPY system/mertensia-setup-shell.conf /usr/share/mertensia/setup-shell.conf
COPY system/mertensia-firstboot-sysusers.conf /usr/lib/sysusers.d/mertensia-firstboot.conf
RUN systemd-sysusers /usr/lib/sysusers.d/mertensia-firstboot.conf
COPY system/homed.conf /etc/systemd/homed.conf
COPY system/gdm-custom.conf /etc/gdm/custom.conf
COPY system/mertensia-firstboot.desktop /etc/xdg/autostart/mertensia-firstboot.desktop
COPY system/mertensia-accounts.desktop /usr/share/applications/org.mertensia.Accounts.desktop
COPY system/org.mertensia.Accounts.policy /usr/share/polkit-1/actions/org.mertensia.Accounts.policy
COPY system/49-mertensia-accounts.rules /usr/share/polkit-1/rules.d/49-mertensia-accounts.rules
COPY system/mertensia-setup-authorized /usr/libexec/mertensia-setup-authorized
COPY system/accountsservice-mertensia-setup /usr/share/mertensia/accountsservice-mertensia-setup
COPY system/mertensia-firstboot-tmpfiles.conf /usr/lib/tmpfiles.d/mertensia-firstboot.conf
COPY system/mertensia-firstboot-retire.path /usr/lib/systemd/system/mertensia-firstboot-retire.path
COPY system/mertensia-firstboot-retire.service /usr/lib/systemd/system/mertensia-firstboot-retire.service
COPY system/mertensia-firstboot-finish.timer /usr/lib/systemd/system/mertensia-firstboot-finish.timer
COPY system/mertensia-firstboot-finish.service /usr/lib/systemd/system/mertensia-firstboot-finish.service
COPY system/mertensia_firstboot.py /usr/bin/mertensia-firstboot
COPY installer/branding /usr/share/mertensia/branding
COPY installer/branding/fonts /usr/share/fonts/mertensia
COPY system/mertensia_accounts_backend.py /usr/libexec/mertensia-accounts-helper
COPY system/mertensia_sync_login_users.py /usr/libexec/mertensia-sync-login-users
COPY system/mertensia-login-users.service /usr/lib/systemd/system/mertensia-login-users.service
COPY system/mertensia-login-users.path /usr/lib/systemd/system/mertensia-login-users.path
RUN systemctl enable mertensia-login-users.service mertensia-login-users.path
COPY system/mertensia-reseal-root /usr/bin/mertensia-reseal-root
RUN chmod 0755 \
      /usr/bin/mertensia-firstboot \
      /usr/bin/mertensia-reseal-root \
      /usr/libexec/mertensia-setup-authorized \
      /usr/libexec/mertensia-accounts-helper && \
    chmod 0600 /usr/share/mertensia/accountsservice-mertensia-setup && \
    systemctl enable mertensia-firstboot-retire.path

## Clean up temporary files
RUN dnf clean all && \
    rm -rf /run/dnf /run/tuned /var/lib/dnf/repos && \
    rm -f /var/cache/ldconfig/aux-cache \
          /var/cache/ibus/bus/registry \
          /var/cache/swcatalog/cache/*.xb && \
    find /var/log -type f -delete

## MertensiaOS branding
COPY installer/os-release /usr/lib/os-release
COPY installer/system-release /usr/lib/mertensiaos-release
RUN ln -sfn ../usr/lib/mertensiaos-release /etc/system-release && \
    ln -sfn ../usr/lib/mertensiaos-release /etc/redhat-release && \
    ln -sfn ../usr/lib/mertensiaos-release /etc/fedora-release

## Trust the project's release key for installation and subsequent bootc updates.
COPY cosign.pub /etc/pki/containers/mertensia.pub
COPY system/containers-policy.json /etc/containers/policy.json
COPY system/containers-policy.json /usr/share/mertensia/bootc-policy.json
COPY system/mertensia-registries.yaml /etc/containers/registries.d/mertensia.yaml

## Keep the payload initramfs generic and include early LUKS/TPM discovery.
RUN kernel="$(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -n1)" && \
    test -n "$kernel" && \
    mkdir -p "$(realpath /root)" && \
    DRACUT_NO_XATTR=1 dracut --force --no-hostonly --zstd --add "crypt systemd systemd-cryptsetup tpm2-tss" \
      "/usr/lib/modules/${kernel}/initramfs.img" "$kernel" && \
    test -s "/usr/lib/modules/${kernel}/initramfs.img" && \
    lsinitrd -m "/usr/lib/modules/${kernel}/initramfs.img" | grep -Fx tpm2-tss

RUN bootc container lint --fatal-warnings --no-truncate
