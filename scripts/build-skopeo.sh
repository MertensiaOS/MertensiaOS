#!/usr/bin/env bash
# Build the Fedora Skopeo RPM with support for a pinned Fulcio workflow identity.
set -euo pipefail
mode="${1:?Supply locked or refresh mode}"

build_root=/build/skopeo
mkdir -p "$build_root" /out
python3 /build/locked-rpms.py "$mode" skopeo-source source "$build_root" skopeo
sources=("$build_root"/skopeo-*.src.rpm)
test "${#sources[@]}" -eq 1
release="$(rpm -qp --qf '%{RELEASE}' "${sources[0]}").mertensia1"
rpm --define "_topdir $build_root" -i "${sources[0]}"
install -m0644 /build/skopeo-fulcio-build-signer.patch "$build_root/SOURCES/"

python3 - "$build_root/SPECS/skopeo.spec" "$release" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
spec, count = re.subn(r"^Release:.*$", "Release: " + sys.argv[2], path.read_text(), flags=re.MULTILINE)
if count != 1 or "%autosetup" not in spec or "\n%description\n" not in spec:
    raise SystemExit("Skopeo spec changed; review the workflow identity patch")
path.write_text(spec.replace("\n%description\n", "\nPatch9999: skopeo-fulcio-build-signer.patch\n\n%description\n", 1))
PY

python3 /build/locked-rpms.py "$mode" skopeo-builddep builddep "$build_root/SPECS/skopeo.spec"
# Keep Go's build cache in the writable RPM workspace even if /root is unavailable.
GOCACHE="$build_root/go-build-cache" \
    rpmbuild --define "_topdir $build_root" -bb "$build_root/SPECS/skopeo.spec"
cp "$build_root"/RPMS/*/skopeo-[0-9]*.rpm /out/
