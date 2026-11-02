#!/usr/bin/env bash
# Retain Fedora packaging while replacing local account management with ours.
set -euo pipefail

build_root=/build/gnome-control-center
mkdir -p "$build_root" /out
dnf download --source --destdir="$build_root" gnome-control-center
sources=("$build_root"/gnome-control-center-*.src.rpm)
test "${#sources[@]}" -eq 1
release="$(rpm -qp --qf '%{RELEASE}' "${sources[0]}").mertensia1"
rpm --define "_topdir $build_root" -i "${sources[0]}"
install -m0644 /build/gnome-control-center-mertensia-accounts.patch "$build_root/SOURCES/"
install -m0644 /build/gnome/cc-mertensia-accounts.{c,h} "$build_root/SOURCES/"

python3 - "$build_root/SPECS/gnome-control-center.spec" "$release" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
spec = path.read_text()
spec, count = re.subn(r"^Release:.*$", "Release: " + sys.argv[2], spec, flags=re.MULTILINE)
if count != 1 or "\n%description\n" not in spec:
    raise SystemExit("GNOME Settings spec changed; review the Accounts redirect build")
spec = spec.replace("\n%description\n", "\nSource9998: cc-mertensia-accounts.c\n"
                    "Source9999: cc-mertensia-accounts.h\n"
                    "Patch9999: gnome-control-center-mertensia-accounts.patch\n\n%description\n", 1)
spec, count = re.subn(r"^(%autosetup[^\n]*)$", r"\1\n"
                     "install -m0644 %{SOURCE9998} %{SOURCE9999} panels/common/", spec, flags=re.MULTILINE)
if count != 1:
    raise SystemExit("GNOME Settings preparation changed; review the Accounts redirect build")
path.write_text(spec)
PY

dnf -y builddep "$build_root/SPECS/gnome-control-center.spec"
rpmbuild --define "_topdir $build_root" -bb "$build_root/SPECS/gnome-control-center.spec"
cp "$build_root"/RPMS/*/gnome-control-center-[0-9]*.rpm /out/
# Some Fedora releases split directory ownership into a matching subpackage.
for package in "$build_root"/RPMS/*/gnome-control-center-filesystem-[0-9]*.rpm; do
    if test -f "$package"; then
        cp "$package" /out/
    fi
done
