#!/usr/bin/python3
"""Render and exercise Mertensia System using only in-memory update/account fixtures."""

import argparse
import copy
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from accounts import Fixture
from mertensia.accounts.ui import AccountsView, Adw, Gio, GLib, Gtk
from mertensia.system.ui import SystemWindow
from mertensia.updates.backend import UpdateError
from mertensia.updates.ui import UpdatesView
from gi.repository import Graphene


class UpdatesFixture:
    def __init__(self):
        self.state = {"record": {"version": 1, "booted": {"version": "45.20261105.1"}}, "current": True,
                      "supported": True, "running": False, "failed": False, "timer_enabled": True, "timer_active": True}
        self.operations = []
        self.reject = False

    def load(self):
        return copy.deepcopy(self.state)

    def request(self, operation):
        if self.reject:
            raise UpdateError("Authentication was cancelled. You can try again.")
        self.operations.append(operation)
        if operation == "download":
            self.state["record"].update(staged_digest="sha256:" + "b" * 64,
                staged={"version": "45.20261105.2"}, last_successful_check="2026-11-05T00:00:00+00:00")
        elif operation == "resume":
            self.state["record"].pop("recovery_pending", None)
        else:
            self.state["timer_enabled"] = self.state["timer_active"] = operation == "enable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--width", type=int, default=780)
    parser.add_argument("--height", type=int, default=760)
    args = parser.parse_args()
    accounts, updates = Fixture(), UpdatesFixture()
    errors = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("System preview attempted to run a host command")

    # Fixture callbacks supply every system interaction, including restart.
    subprocess.run = subprocess.Popen = forbidden
    app = Adw.Application(application_id="org.mertensia.System.Preview", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if args.theme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        window = SystemWindow(app, accounts_factory=lambda: AccountsView(
            client_factory=lambda: accounts, admin_request=accounts.admin_request,
            identity_factory=lambda: accounts.identity, languages_factory=lambda: ["en_NZ.utf8"]),
            updates_factory=lambda: UpdatesView(load=updates.load, request=updates.request, restart=lambda: None))
        window.set_default_size(args.width, args.height)
        window.present()
        if not args.capture:
            return
        args.capture.mkdir(parents=True, exist_ok=True)
        steps = iter(("idle", "ready", "accounts", "add-user", "paused", "resume", "disabled", "cancelled",
                      "deferred", "failed", "downloading", "interrupted", "stale", "unsupported"))
        view = window.updates

        def capture(name):
            target = window.accounts.active_form.get_child() if window.accounts.active_form else window
            paintable = Gtk.WidgetPaintable.new(target)
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, target.get_width(), target.get_height())
            node = snapshot.to_node()
            if node is None:
                window.queue_draw()
                return False
            filled = Gtk.Snapshot.new()
            found, background = window.get_style_context().lookup_color("window_bg_color")
            assert found
            filled.append_color(background, Graphene.Rect().init(0, 0, target.get_width(), target.get_height()))
            filled.append_node(node)
            texture = window.get_renderer().render_texture(filled.to_node(), None)
            path = args.capture / f"system-{args.theme}-{name}.png"
            assert texture.save_to_png(str(path))
            print(path, flush=True)
            return True

        def advance():
            if view.busy or window.accounts.busy or not window.accounts.loaded:
                return GLib.SOURCE_CONTINUE
            name = next(steps, None)
            if name is None:
                assert updates.operations == ["download", "resume", "disable"]
                print("PASS: download, staged version, navigation, account dialog, recovery acknowledgement, "
                      "timer control, authorization cancellation and truthful status states", flush=True)
                window.close()
                app.quit()
                return GLib.SOURCE_REMOVE
            if name == "idle":
                assert view.banner.get_title() == "No update staged"
                assert view.download_button.get_sensitive()
            elif name == "ready":
                view.download_button.emit("clicked")
            elif name == "accounts":
                assert view.banner.get_title() == "Ready to restart"
                assert view.restart_button.get_visible()
                assert not view.download_button.get_sensitive()
                window.show_page("accounts")
            elif name == "add-user":
                window.show_page("accounts", add_user=True)
                assert window.accounts.active_form is not None
            elif name == "paused":
                window.accounts.active_form.close()
                window.show_page("updates")
                updates.state["record"].pop("staged_digest")
                updates.state["record"].update(recovery_pending="rollback-preview")
                view.refresh()
            elif name == "resume":
                assert view.resume_button.get_visible()
                assert not view.download_button.get_sensitive()
                view.confirm_resume()
                dialog = window.get_visible_dialog()
                assert dialog is not None
                dialog.emit("response", "resume")
                dialog.close()
            elif name == "disabled":
                assert not view.resume_button.get_visible()
                view.automatic.set_active(False)
            elif name == "cancelled":
                assert not view.automatic.get_active()
                updates.reject = True
                view.automatic.set_active(True)
                assert not view.automatic.get_active()
            else:
                updates.reject = False
                updates.state["record"].pop("recovery_pending", None)
                updates.state["record"].update(activity="idle", reason=None)
                updates.state.update(running=False, failed=False)
                if name == "deferred":
                    updates.state["record"].update(activity="deferred", reason="Waiting for an unmetered internet connection")
                elif name == "failed":
                    updates.state["failed"] = True
                elif name == "downloading":
                    updates.state["running"] = True
                elif name == "interrupted":
                    updates.state["record"]["activity"] = "downloading"
                elif name == "stale":
                    updates.state.update(current=False, record={})
                elif name == "unsupported":
                    updates.state["supported"] = False
                view.refresh()

            def after():
                if view.busy or window.accounts.busy:
                    return GLib.SOURCE_CONTINUE
                if name == "cancelled":
                    assert view.state is None
                    assert not view.automatic.get_active()
                    assert not view.download_button.get_sensitive()
                if not capture(name):
                    return GLib.SOURCE_CONTINUE
                GLib.timeout_add(250, checked_advance)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(350, lambda: checked(after))
            return GLib.SOURCE_REMOVE

        def checked(callback):
            try:
                return callback()
            except Exception as error:
                errors.append(error)
                app.quit()
                return GLib.SOURCE_REMOVE

        def checked_advance():
            return checked(advance)

        GLib.timeout_add(350, checked_advance)

        def timed_out():
            errors.append(RuntimeError("System preview timed out"))
            app.quit()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(30, timed_out)

    def checked_activate(app):
        try:
            activate(app)
        except Exception as error:
            errors.append(error)
            app.quit()
    app.connect("activate", checked_activate)
    app.run([])
    if errors:
        raise errors[0]


if __name__ == "__main__":
    main()
