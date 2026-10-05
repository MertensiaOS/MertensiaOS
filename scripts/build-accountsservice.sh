#!/usr/bin/env bash
# Rebuild Fedora's RPMs with the homed enumeration fix in a disposable stage.
set -euo pipefail

build_root=/build/accountsservice
mkdir -p "$build_root" /out
dnf download --source --destdir="$build_root" accountsservice
sources=("$build_root"/accountsservice-*.src.rpm)
test "${#sources[@]}" -eq 1
release="$(rpm -qp --qf '%{RELEASE}' "${sources[0]}").mertensia1"
rpm --define "_topdir $build_root" -i "${sources[0]}"
install -m0644 /build/accountsservice-homed-enumeration.patch "$build_root/SOURCES/"

# Source RPMs contain a concrete Release. Keep Fedora's build configuration
# and package relationships, giving both daemon and library the patched release.
python3 - "$build_root/SPECS/accountsservice.spec" "$release" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
spec = path.read_text()
spec, count = re.subn(r"^Release:.*$", "Release: " + sys.argv[2], spec, flags=re.MULTILINE)
if count != 1 or "%autosetup" not in spec or "\n%description\n" not in spec:
    raise SystemExit("AccountsService spec changed; review the downstream patch build")
spec = spec.replace("\n%description\n", "\nPatch9999: accountsservice-homed-enumeration.patch\n\n%description\n", 1)
path.write_text(spec)
PY

dnf -y builddep "$build_root/SPECS/accountsservice.spec"
rpmbuild --define "_topdir $build_root" -bb "$build_root/SPECS/accountsservice.spec"
cp "$build_root"/RPMS/*/accountsservice-[0-9]*.rpm \
   "$build_root"/RPMS/*/accountsservice-libs-[0-9]*.rpm /out/
