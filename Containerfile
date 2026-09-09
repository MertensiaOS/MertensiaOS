FROM quay.io/fedora/fedora-bootc:45

## Core packages
RUN dnf -y install \
    openssl \
    fwupd \
    && dnf clean all

## GNOME
RUN dnf -y install \
    gdm \
    gnome-shell \
    gnome-session \
    gnome-control-center \
    nautilus \
    NetworkManager \
    xdg-desktop-portal \
    xdg-desktop-portal-gnome \
    && dnf clean all

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

RUN dnf clean all && \
    rm -rf /run/dnf /run/tuned /var/lib/dnf/repos && \
    rm -f /var/cache/ldconfig/aux-cache \
          /var/cache/ibus/bus/registry \
          /var/cache/swcatalog/cache/*.xb && \
    find /var/log -type f -delete

RUN bootc container lint --fatal-warnings --no-truncate
