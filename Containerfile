FROM quay.io/fedora/fedora-bootc:45 AS accountsservice-build

RUN dnf -y install dnf5-plugins rpm-build gcc pam-devel

COPY system/patches/accountsservice-homed-enumeration.patch /build/accountsservice-homed-enumeration.patch

COPY scripts/build-accountsservice.sh /build/build-accountsservice.sh

RUN bash /build/build-accountsservice.sh

COPY system/pam/pam_mertensia_password.c /build/pam_mertensia_password.c
RUN mkdir -p /pam-out && gcc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared \
    -Wl,-z,relro,-z,now,-z,noexecstack -o /pam-out/pam_mertensia_password.so \
    /build/pam_mertensia_password.c -lpam

FROM quay.io/fedora/fedora-bootc:45 AS gnome-control-center-build

RUN dnf -y install dnf5-plugins rpm-build
COPY system/patches/gnome-control-center-mertensia-accounts.patch /build/gnome-control-center-mertensia-accounts.patch
COPY system/gnome /build/gnome
COPY scripts/build-gnome-control-center.sh /build/build-gnome-control-center.sh
RUN bash /build/build-gnome-control-center.sh

## Stage file inputs on an empty filesystem to avoid diffing the full OS for each COPY.
FROM scratch AS payload-files
COPY system /system
COPY branding /branding
COPY cosign.pub /cosign.pub

FROM scratch AS payload-applications
COPY src/mertensia/__init__.py /usr/lib/mertensia/python/mertensia/__init__.py
COPY src/mertensia/accounts /usr/lib/mertensia/python/mertensia/accounts
COPY src/mertensia/firstboot /usr/lib/mertensia/python/mertensia/firstboot
COPY src/mertensia/ui /usr/lib/mertensia/python/mertensia/ui
COPY src/mertensia/boot_health /usr/lib/mertensia/python/mertensia/boot_health
COPY src/mertensia/updates /usr/lib/mertensia/python/mertensia/updates

FROM scratch AS payload-build-scripts
COPY scripts/configure-payload.sh scripts/install-image-files.py scripts/clean-desktop.py scripts/check-desktop-policy.py scripts/payload-files.json /

FROM quay.io/fedora/fedora-bootc:45

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
    oo7-daemon \
    pam_oo7 \
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

COPY --from=accountsservice-build /out/ /tmp/mertensia-accountsservice/
COPY --from=gnome-control-center-build /out/ /tmp/mertensia-gnome-control-center/
COPY --from=accountsservice-build /pam-out/pam_mertensia_password.so /usr/lib64/security/pam_mertensia_password.so

RUN dnf -y install \
    gdm \
    /tmp/mertensia-accountsservice/*.rpm \
    gnome-shell \
    gnome-session \
    /tmp/mertensia-gnome-control-center/*.rpm \
    nautilus \
    NetworkManager \
    xdg-desktop-portal \
    xdg-desktop-portal-gnome \
    && rm -rf /tmp/mertensia-accountsservice /tmp/mertensia-gnome-control-center && dnf clean all

RUN authselect select local with-systemd-homed with-group-merging --force --nobackup && \
    authselect check && \
    install -Dm0600 /var/lib/authselect/checksum /usr/share/mertensia/authselect/checksum && \
    rm -f /var/lib/authselect/checksum && \
    systemctl enable systemd-homed.service

RUN systemctl enable NetworkManager.service && \
    systemctl enable gdm.service && \
    systemctl set-default graphical.target

RUN dnf -y install \
    chromium \
    flatpak \
    gnome-software \
    ptyxis \
    gnome-text-editor \
    gnome-firmware \
    && dnf clean all

RUN --mount=type=bind,from=payload-files,target=/run/mertensia-files,ro \
    --mount=type=bind,from=payload-build-scripts,target=/run/mertensia-build,ro \
    python3 /run/mertensia-build/install-image-files.py \
      /run/mertensia-build/payload-files.json /run/mertensia-files / && \
    bash /run/mertensia-build/configure-payload.sh

## Application edits reuse the configured OS and initramfs.
COPY --from=payload-applications / /
RUN bootc container lint --fatal-warnings --no-truncate
