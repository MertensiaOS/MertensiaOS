#!/usr/bin/python3
"""Preview the real GTK installer using fixtures, without disk or reboot access."""

import argparse
import importlib.util
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "installer_ui", Path(__file__).resolve().parents[1] / "installer/mertensia_installer.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
Adw, Gio, GLib, Gtk = ui.Adw, ui.Gio, ui.GLib, ui.Gtk

PAGES = ("welcome", "requirements", "disk", "confirm", "progress", "recovery", "finish", "failure")
DISKS = [{"model": "Example NVMe SSD", "size": 512 * 1024**3,
          "stable_path": "/dev/disk/by-id/nvme-EXAMPLE_512GB", "serial": "PREVIEW-001"},
         {"model": "Example SATA SSD", "size": 1024**4,
          "stable_path": "/dev/disk/by-id/ata-EXAMPLE_1TB", "serial": "PREVIEW-002"}]
KEY = "ABCDEF-GHJKLM-NPQRST-UVWXYZ-234567-89ABCD-EFGHJK-LMNPQR"


class PreviewWindow(ui.InstallerWindow):
    def _helper_once(self, command, callback):
        data = ({"ok": True, "architecture_ok": True, "uefi": True,
                 "secure_boot": True, "tpm2": True} if command == "probe"
                else {"event": "disks", "disks": DISKS})
        GLib.idle_add(callback, data)

    def _install(self):
        self.stack.set_visible_child_name("progress")
        self._event({"event": "phase", "message": "Copying the system image", "progress": 0.42})
        GLib.timeout_add(1800, self._event, {"event": "recovery-key", "key": KEY})

    def _confirm_recovery(self, *_args):
        self._event({"event": "complete"})

    def _save_recovery(self, *_args):
        dialog = Adw.AlertDialog(heading="Preview only", body="The sample key is not written to disk.")
        dialog.add_response("close", "Close")
        dialog.present(self)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", choices=PAGES, default="welcome")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=760)
    parser.add_argument("--capture", type=Path, help="Render every page to PNG and exit")
    args = parser.parse_args()
    app = Adw.Application(application_id="org.mertensia.Installer.Preview",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    errors = []

    def activate(app):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if args.theme == "dark" else Adw.ColorScheme.FORCE_LIGHT
        )
        window = PreviewWindow(app)
        window.unfullscreen()
        window.set_default_size(args.width, args.height)
        window.set_title("Installer preview - sample data")
        theme = Gtk.IconTheme.get_for_display(window.get_display())
        for path in (ui.BRANDING / "icons").glob("*.svg"):
            assert theme.has_icon(path.stem), f"Missing icon: {path.stem}"
            icon = theme.lookup_icon(path.stem, None, 24, 1, Gtk.TextDirection.LTR,
                                     Gtk.IconLookupFlags.FORCE_SYMBOLIC)
            assert icon.is_symbolic(), f"Not symbolic: {path.stem}"
            assert icon.get_file().get_basename() == path.name, f"Substituted icon: {path.stem}"
        window._probe_done({"ok": True, "architecture_ok": True, "uefi": True,
                            "secure_boot": True, "tpm2": True})
        window._disks_done({"event": "disks", "disks": DISKS})
        window.disk_list.select_row(window.disk_list.get_row_at_index(0))
        window._show_confirmation()
        window._event({"event": "phase", "message": "Copying the system image", "progress": 0.42})
        window._event({"event": "recovery-key", "key": KEY})
        window.install_disk = "Example NVMe SSD at /dev/disk/by-id/nvme-EXAMPLE_512GB"
        window._show_failure("The example TPM could not enroll the root volume. This is a preview diagnostic.")
        window.disk_list.unselect_all()
        assert not window.disk_next.get_sensitive()
        window.disk_list.select_row(window.disk_list.get_row_at_index(0))
        assert window.disk_next.get_sensitive()
        for value, enabled in (("erase", False), ("ERASE", True), ("", False)):
            window.erase_entry.set_text(value)
            assert window.install_button.get_sensitive() == enabled
        for confirmed in (True, False):
            window.recovery_check.set_active(confirmed)
            assert window.recovery_continue.get_sensitive() == confirmed
        window.stack.set_visible_child_name(args.page)
        window.present()
        if args.capture:
            args.capture.mkdir(parents=True, exist_ok=True)
            pages = iter(PAGES)
            window.stack.set_transition_duration(0)

            def capture(page, bottom=False):
                try:
                    paintable = Gtk.WidgetPaintable.new(window)
                    snapshot = Gtk.Snapshot.new()
                    paintable.snapshot(snapshot, window.get_width(), window.get_height())
                    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
                    suffix = "-bottom" if bottom else ""
                    path = args.capture / f"{args.theme}-{args.width}-{page}{suffix}.png"
                    assert texture.save_to_png(str(path))
                    print(path, flush=True)
                    adjustment = window.stack.get_visible_child().get_vadjustment()
                    if not bottom and adjustment.get_upper() > adjustment.get_page_size():
                        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                        GLib.timeout_add(250, capture, page, True)
                        return False
                    advance()
                except Exception as error:
                    errors.append(error)
                    app.quit()
                return False

            def advance():
                page = next(pages, None)
                if page is None:
                    app.quit()
                    return False
                window.stack.set_visible_child_name(page)
                GLib.timeout_add(250, capture, page)
                return False

            GLib.timeout_add(300, advance)

    app.connect("activate", activate)
    # Fail closed if a future UI change bypasses the preview overrides.
    with mock.patch.object(ui.subprocess, "Popen", side_effect=RuntimeError("Preview blocks processes")), \
            mock.patch.object(ui.subprocess, "run", side_effect=RuntimeError("Preview blocks processes")):
        app.run([])
    if errors:
        raise errors[0]


if __name__ == "__main__":
    main()
