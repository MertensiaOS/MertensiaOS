#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:?Usage: validate-image.sh LOCAL_PAYLOAD_IMAGE}"
for command in podman openssl sha256sum python3; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
EXPECTED_KEY_SHA256="$(openssl pkey -pubin -in "$ROOT_DIR/cosign.pub" -outform DER | sha256sum)"
EXPECTED_KEY_SHA256="${EXPECTED_KEY_SHA256%% *}"
EXPECTED_POLICY="$(<"$ROOT_DIR/system/config/containers/policy.json")"
EXPECTED_REGISTRIES_SHA256="$(sha256sum "$ROOT_DIR/system/config/containers/registries.yaml")"
EXPECTED_REGISTRIES_SHA256="${EXPECTED_REGISTRIES_SHA256%% *}"
EXPECTED_BROWSER_MANAGED="$(<"$ROOT_DIR/system/config/chromium/policies/managed/mertensia.json")"
EXPECTED_BROWSER_RECOMMENDED="$(<"$ROOT_DIR/system/config/chromium/policies/recommended/mertensia.json")"
# Pass only public trust data as arguments, without binding host paths into the
# image. This also works on SELinux hosts and keeps the expected trust external.
podman run --rm --entrypoint /usr/bin/bash "$IMAGE" -euc "$(cat <<'BASH'
  set -o pipefail
  bootc container lint --fatal-warnings --no-truncate
  test "$(plymouth-set-default-theme)" = mertensia
  test "$(fc-match -f '%{file}' Figtree)" = /usr/share/fonts/mertensia/Figtree-Variable.ttf
  for initrd in /usr/lib/modules/*/initramfs.img; do
    /usr/libexec/mertensia-check-plymouth-initrd "$initrd"
  done
  shopt -s nullglob
  for test_artifact in \
      /usr/lib/systemd/system/mertensia-integration* \
      /usr/lib/systemd/system/greenboot-healthcheck.service.d/*mertensia-integration* \
      /usr/lib/greenboot/check/required.d/*mertensia-integration* \
      /usr/lib/greenboot/check/wanted.d/*mertensia-integration* \
      /etc/greenboot/check/required.d/*mertensia-integration* \
      /etc/greenboot/check/wanted.d/*mertensia-integration* \
      /usr/libexec/mertensia-integration* \
      /etc/systemd/system/mertensia-integration* \
      /etc/systemd/system/*.wants/mertensia-integration* \
      /usr/lib/bootc/install/*mertensia-integration* \
      /usr/share/mertensia-integration \
      /usr/share/mertensia-flatpak-tests; do
    if [[ -e "$test_artifact" || -L "$test_artifact" ]]; then
      echo "Refusing to release an integration artifact: $test_artifact" >&2
      exit 1
    fi
  done
  test -x /usr/libexec/mertensia-boot-health
  test -x /usr/libexec/mertensia-boot-health-notify
  test -x /usr/bin/mertensia-update
  test -L /etc/systemd/system/timers.target.wants/bootc-fetch-apply-updates.timer
  test -L /etc/systemd/system/greenboot-success.target.wants/mertensia-update-status.service
  test -L /etc/systemd/system/bootc-status-updated.target.wants/mertensia-update-status.service
  test -L /etc/systemd/user/gnome-session.target.wants/gnome-software.service
  rpm -q gnome-software
  rpm -q chromium chromium-common
  test -x /usr/bin/chromium-browser
  test -x /usr/lib64/chromium-browser/chrome-sandbox
  grep -Fx '. /etc/chromium/mertensia-wayland.conf' /etc/chromium/chromium.conf
  test -f /usr/lib64/gnome-software/plugins-23/libgs_plugin_flatpak.so
  if find /usr/lib64/gnome-software -name 'libgs_plugin_*.so' | grep -E '/libgs_plugin_(packagekit|dnf5|rpm-ostree|fwupd|epiphany|dkms)\.so$'; then
    echo "Payload includes an excluded GNOME Software update backend" >&2
    exit 1
  fi
  test ! -e /usr/share/flatpak/preinstall.d/mertensia.preinstall
  test ! -e /usr/lib/systemd/system/mertensia-flatpak-preinstall.timer
  test "$(gsettings get org.gnome.software download-updates)" = true
  test "$(gsettings get org.gnome.software download-updates-notify)" = true
  test "$(gsettings get org.gnome.software refresh-when-metered)" = false
  test "$(gsettings get org.gnome.software allow-updates)" = true
  grep -Fx 'ExecStart=/usr/bin/mertensia-update stage' /usr/lib/systemd/system/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf
  if grep -E -- '--apply|systemctl.*reboot' /usr/lib/systemd/system/bootc-fetch-apply-updates.service.d/50-mertensia-stage.conf; then
    echo "Automatic update override requests a restart" >&2
    exit 1
  fi
  test -x /usr/lib/greenboot/check/required.d/50-mertensia-desktop.sh
  test -s /usr/lib/bootupd/grub2-static/configs.d/08_greenboot.cfg
  test -L /etc/systemd/system/graphical.target.wants/greenboot-healthcheck.service
  test ! -L /etc/systemd/system/multi-user.target.wants/greenboot-healthcheck.service
  test -L /etc/systemd/system/ostree-finalize-staged.service.requires/greenboot-set-rollback-trigger.service
  test -L /etc/systemd/system/greenboot-success.target.wants/mertensia-boot-health-status.service
  test "$(readlink /etc/systemd/user/grub-boot-success.timer)" = /dev/null
  for test_user in testing integration_admin integration_user; do
    if getent passwd "$test_user" >/dev/null; then
      echo "Refusing to release a development image with account $test_user" >&2
      exit 1
    else
      account_status=$?
      if [[ "$account_status" != 2 ]]; then
        echo "Could not check the image for development account $test_user" >&2
        exit 1
      fi
    fi
  done
  payload_key_sha256="$(openssl pkey -pubin -in /etc/pki/containers/mertensia.pub -outform DER | sha256sum)"
  if [[ "${payload_key_sha256%% *}" != "$1" ]]; then
    echo "Payload signing key does not match the release trust root" >&2
    exit 1
  fi
  python3 - "$2" "$3" "$4" "$5" <<'PY'
import configparser
import hashlib
import json
from pathlib import Path
import sys

expected_owner = 0
browser_root = Path("/etc/chromium/policies")
for level, expected in (("managed", sys.argv[3]), ("recommended", sys.argv[4])):
    policy_path = browser_root / level / "mertensia.json"
    actual_values = json.loads(policy_path.read_text())
    expected_values = json.loads(expected)
    if (actual_values != expected_values or
            any(type(actual_values[key]) is not type(value) for key, value in expected_values.items())):
        raise SystemExit(f"Chromium {level} policies differ from release defaults")
    for path in (browser_root, policy_path.parent, policy_path):
        metadata = path.stat()
        mode = 0o755 if path.is_dir() else 0o644
        if path.is_symlink() or metadata.st_uid != expected_owner or metadata.st_gid != expected_owner or metadata.st_mode & 0o7777 != mode:
            raise SystemExit(f"Unsafe Chromium policy ownership or permissions: {path}")
desktop = configparser.ConfigParser(interpolation=None)
desktop.read("/usr/share/applications/chromium-browser.desktop")
if desktop.get("Desktop Entry", "Exec", fallback="").split()[0:1] != ["/usr/bin/chromium-browser"]:
    raise SystemExit("Chromium desktop entry does not use the Fedora launcher")
handlers = configparser.ConfigParser(interpolation=None)
handlers.read("/usr/share/applications/gnome-mimeapps.list")
for mime in ("x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml"):
    if handlers.get("Default Applications", mime, fallback="") != "chromium-browser.desktop;":
        raise SystemExit(f"Missing Chromium default handler: {mime}")
expected_policy = json.loads(sys.argv[1])
if "GREENBOOT_MAX_BOOT_ATTEMPTS=3" not in Path("/etc/greenboot/greenboot.conf").read_text().splitlines():
    raise SystemExit("Payload does not use the release Greenboot retry policy")
for policy_file in ("/etc/containers/policy.json", "/usr/share/mertensia/bootc-policy.json"):
    policy = json.loads(Path(policy_file).read_text())
    if policy != expected_policy:
        raise SystemExit(f"Payload signature policy differs from the release policy: {policy_file}")
registries = Path("/etc/containers/registries.d/mertensia.yaml").read_bytes()
if hashlib.sha256(registries).hexdigest() != sys.argv[2]:
    raise SystemExit("Payload signature registry configuration differs from the release configuration")
PY
BASH
)" mertensia-image-validation "$EXPECTED_KEY_SHA256" "$EXPECTED_POLICY" "$EXPECTED_REGISTRIES_SHA256" "$EXPECTED_BROWSER_MANAGED" "$EXPECTED_BROWSER_RECOMMENDED"
