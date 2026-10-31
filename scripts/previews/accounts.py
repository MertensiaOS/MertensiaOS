#!/usr/bin/python3
"""Exercise and render the real Accounts UI with in-memory users, never the host."""
import argparse
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mertensia.accounts import client, ui
from gi.repository import Graphene


class Fixture:
    def __init__(self):
        self.identity = client.Identity("alex", 1000, True)
        self.users = [client.Account("alex", 1000, "Alex Morgan", True, False, True, "en_NZ.utf8", "", "active"),
                      client.Account("sam", 1001, "Sam Rivera", False, False, False, "", "", "inactive")]
        self.password = "example old password"

    def accounts(self):
        return list(self.users)

    def change_password(self, username, current, password):
        assert username == self.identity.username
        if current != self.password:
            raise client.RequestError("The current password is incorrect. Try again.")
        self.password = password

    def set_profile(self, username, field, value):
        field = {"name": "name", "language": "language", "avatar": "avatar"}[field]
        self.users = [replace(user, **{field: value}) if user.username == username else user for user in self.users]

    def admin_request(self, request):
        assert self.identity.administrator
        operation = request.get("operation", "create")
        if operation == "create":
            self.users.append(client.Account(request["username"], 1002, request["real_name"], request["admin"],
                                             False, False, "", "", "inactive"))
        elif operation == "remove":
            assert request["confirm_username"] == request["username"]
            self.users = [user for user in self.users if user.username != request["username"]]
        else:
            field = "administrator" if operation == "set-admin" else "locked"
            self.users = [replace(user, **{field: request["enabled"]}) if user.username == request["username"] else user
                          for user in self.users]
        return {"event": "complete", "ok": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    fixture = Fixture()
    app = ui.Adw.Application(application_id="org.mertensia.Accounts.Preview", flags=ui.Gio.ApplicationFlags.NON_UNIQUE)
    errors = []

    def activate(app):
        ui.Adw.StyleManager.get_default().set_color_scheme(
            ui.Adw.ColorScheme.FORCE_DARK if args.theme == "dark" else ui.Adw.ColorScheme.FORCE_LIGHT)
        window = ui.AccountsWindow(app, client_factory=lambda: fixture, admin_request=fixture.admin_request,
                                   identity_factory=lambda: fixture.identity,
                                   languages_factory=lambda: ["en_NZ.utf8", "en_US.utf8", "fr_FR.utf8"])
        window.present()
        if not args.capture:
            return
        args.capture.mkdir(parents=True, exist_ok=True)
        actions = iter(("overview", "profile", "password-error", "password-success", "create", "created", "remove", "removed", "standard"))

        def capture(name):
            # Adw.Dialog may use a separate native surface on Broadway.
            target = window.active_form.get_child() if window.active_form else window
            paintable = ui.Gtk.WidgetPaintable.new(target)
            content = ui.Gtk.Snapshot.new()
            paintable.snapshot(content, target.get_width(), target.get_height())
            node = content.to_node()
            if node is None:
                window.queue_draw()
                return False
            snapshot = ui.Gtk.Snapshot.new()
            found, background = window.get_style_context().lookup_color("window_bg_color")
            assert found
            snapshot.append_color(background, Graphene.Rect().init(0, 0, target.get_width(), target.get_height()))
            snapshot.append_node(node)
            texture = window.get_renderer().render_texture(snapshot.to_node(), None)
            path = args.capture / f"accounts-{args.theme}-{name}.png"
            assert texture.save_to_png(str(path))
            print(path, flush=True)
            return True

        def advance():
            if window.busy or not window.loaded:
                return ui.GLib.SOURCE_CONTINUE
            try:
                name = next(actions, None)
                if name is None:
                    print("PASS: actual GTK password validation/change, creation, deletion and non-admin access", flush=True)
                    app.quit()
                    return ui.GLib.SOURCE_REMOVE
                if name == "profile":
                    window.show_profile("alex")
                elif name == "password-error":
                    window.show_password(fixture.users[0])
                    for key, value in {"current": "wrong password", "password": "example new password",
                                       "confirm": "example new password"}.items():
                        window.active_form.fields[key].set_text(value)
                    window.active_form.submit.emit("clicked")
                elif name == "password-success":
                    assert window.active_form.message.get_visible()
                    assert "incorrect" in window.active_form.message.get_text()
                    window.active_form.fields["current"].set_text(fixture.password)
                    window.active_form.submit.emit("clicked")
                elif name == "create":
                    assert fixture.password == "example new password" and window.active_form is None
                    window.show_create()
                    for key, value in {"real_name": "Taylor Chen", "username": "taylor", "password": "another example password",
                                       "confirm": "another example password"}.items():
                        window.active_form.fields[key].set_text(value)
                elif name == "created":
                    window.active_form.submit.emit("clicked")
                elif name == "remove":
                    assert len(fixture.users) == 3
                    account = next(user for user in fixture.users if user.username == "sam")
                    window.show_remove(account)
                    assert not window.active_form.submit.get_sensitive()
                    window.active_form.fields["confirmation"].set_text("sam")
                    assert window.active_form.submit.get_sensitive()
                elif name == "removed":
                    window.active_form.submit.emit("clicked")
                elif name == "standard":
                    assert not any(user.username == "sam" for user in fixture.users)
                    fixture.identity = client.Identity("taylor", 1002, False)
                    window.navigation.pop_to_tag("accounts")
                    window.refresh()
                def after():
                    try:
                        if window.busy:
                            return ui.GLib.SOURCE_CONTINUE
                        if name == "standard":
                            assert not window.add_button.get_sensitive()
                            window.show_create()
                            assert window.active_form is None
                        if not capture(name):
                            return ui.GLib.SOURCE_CONTINUE
                        ui.GLib.timeout_add(300, advance)
                    except Exception as error:
                        errors.append(error)
                        app.quit()
                    return ui.GLib.SOURCE_REMOVE
                ui.GLib.timeout_add(350, after)
            except Exception as error:
                errors.append(error)
                app.quit()
            return ui.GLib.SOURCE_REMOVE
        ui.GLib.timeout_add(350, advance)
        def timed_out():
            errors.append(RuntimeError("Accounts preview timed out"))
            app.quit()
            return ui.GLib.SOURCE_REMOVE
        ui.GLib.timeout_add_seconds(20, timed_out)

    def activate_checked(app):
        try:
            activate(app)
        except Exception as error:
            errors.append(error)
            app.quit()

    app.connect("activate", activate_checked)
    app.run([])
    if errors:
        raise errors[0]


if __name__ == "__main__":
    main()
