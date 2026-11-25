ARG FEDORA_BASE
ARG RPM_INPUT_MODE=locked

FROM ${FEDORA_BASE} AS accountsservice-build
ARG RPM_INPUT_MODE
COPY scripts/locked-rpms.py /build/locked-rpms.py

RUN python3 /build/locked-rpms.py "$RPM_INPUT_MODE" accountsservice-tools install dnf5-plugins rpm-build gcc pam-devel

COPY system/patches/accountsservice-homed-enumeration.patch /build/accountsservice-homed-enumeration.patch

COPY scripts/build-accountsservice.sh /build/build-accountsservice.sh

RUN bash /build/build-accountsservice.sh "$RPM_INPUT_MODE"

COPY system/pam/pam_mertensia_password.c /build/pam_mertensia_password.c
RUN mkdir -p /pam-out && gcc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared \
    -Wl,-z,relro,-z,now,-z,noexecstack -o /pam-out/pam_mertensia_password.so \
    /build/pam_mertensia_password.c -lpam

FROM ${FEDORA_BASE} AS gnome-control-center-build
ARG RPM_INPUT_MODE
COPY scripts/locked-rpms.py /build/locked-rpms.py

RUN python3 /build/locked-rpms.py "$RPM_INPUT_MODE" gnome-control-center-tools install dnf5-plugins rpm-build
COPY system/patches/gnome-control-center-mertensia-accounts.patch /build/gnome-control-center-mertensia-accounts.patch
COPY system/gnome /build/gnome
COPY scripts/build-gnome-control-center.sh /build/build-gnome-control-center.sh
RUN bash /build/build-gnome-control-center.sh "$RPM_INPUT_MODE"

FROM ${FEDORA_BASE} AS skopeo-build
ARG RPM_INPUT_MODE
COPY scripts/locked-rpms.py /build/locked-rpms.py

RUN python3 /build/locked-rpms.py "$RPM_INPUT_MODE" skopeo-tools install dnf5-plugins rpm-build
COPY system/patches/skopeo-fulcio-build-signer.patch /build/skopeo-fulcio-build-signer.patch
COPY scripts/build-skopeo.sh /build/build-skopeo.sh
RUN bash /build/build-skopeo.sh "$RPM_INPUT_MODE"

## Stage file inputs on an empty filesystem to avoid diffing the full OS for each COPY.
FROM scratch AS payload-files
COPY system /system
COPY branding /branding

FROM scratch AS payload-applications
COPY src/mertensia/__init__.py /usr/lib/mertensia/python/mertensia/__init__.py
COPY src/mertensia/accounts /usr/lib/mertensia/python/mertensia/accounts
COPY src/mertensia/firstboot /usr/lib/mertensia/python/mertensia/firstboot
COPY src/mertensia/ui /usr/lib/mertensia/python/mertensia/ui
COPY src/mertensia/boot_health /usr/lib/mertensia/python/mertensia/boot_health
COPY src/mertensia/updates /usr/lib/mertensia/python/mertensia/updates
COPY src/mertensia/system /usr/lib/mertensia/python/mertensia/system

FROM scratch AS payload-build-scripts
COPY scripts/locked-rpms.py scripts/configure-payload.sh scripts/install-image-files.py scripts/clean-desktop.py scripts/check-desktop-policy.py scripts/payload-files.json /

FROM ${FEDORA_BASE}
ARG RPM_INPUT_MODE
ARG INPUT_LOCK_REVISION
LABEL org.mertensiaos.input-lock="${INPUT_LOCK_REVISION}"

RUN --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" fedora-update upgrade
RUN --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" payload-core install \
    openssl \
    fwupd \
    firewalld \
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
    plymouth-theme-spinner


COPY --from=accountsservice-build /out/ /tmp/mertensia-accountsservice/
COPY --from=gnome-control-center-build /out/ /tmp/mertensia-gnome-control-center/
COPY --from=accountsservice-build /pam-out/pam_mertensia_password.so /usr/lib64/security/pam_mertensia_password.so

RUN --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" payload-desktop install \
    gdm \
    /tmp/mertensia-accountsservice/*.rpm \
    gnome-shell \
    gnome-session \
    /tmp/mertensia-gnome-control-center/*.rpm \
    nautilus \
    NetworkManager \
    xdg-desktop-portal \
    xdg-desktop-portal-gnome \
    && rm -rf /tmp/mertensia-accountsservice /tmp/mertensia-gnome-control-center

RUN --mount=type=bind,from=skopeo-build,source=/out,target=/run/mertensia-skopeo,ro \
    --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" payload-skopeo install /run/mertensia-skopeo/skopeo-*.rpm

RUN authselect select local with-systemd-homed with-group-merging --force --nobackup && \
    authselect check && \
    install -Dm0600 /var/lib/authselect/checksum /usr/share/mertensia/authselect/checksum && \
    rm -f /var/lib/authselect/checksum && \
    systemctl enable systemd-homed.service

RUN systemctl enable NetworkManager.service && \
    systemctl enable gdm.service && \
    systemctl set-default graphical.target

RUN --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" payload-apps install \
    flatpak \
    gnome-software \
    ptyxis \
    gnome-text-editor \
    gnome-firmware


# Only the image build uses Brave's repository. Installed systems use bootc.
RUN --mount=type=bind,from=payload-files,source=/system/config/brave/brave-core.asc,target=/run/mertensia-brave.asc,ro \
    --mount=type=bind,from=payload-build-scripts,source=/locked-rpms.py,target=/run/locked-rpms.py,ro \
    python3 /run/locked-rpms.py "$RPM_INPUT_MODE" brave install brave-origin

RUN --mount=type=bind,from=payload-files,target=/run/mertensia-files,ro \
    --mount=type=bind,from=payload-build-scripts,target=/run/mertensia-build,ro \
    python3 /run/mertensia-build/install-image-files.py \
      /run/mertensia-build/payload-files.json /run/mertensia-files / && \
    bash /run/mertensia-build/configure-payload.sh

## Application edits reuse the configured OS and initramfs.
COPY --from=payload-applications / /
RUN bootc container lint --fatal-warnings --no-truncate
