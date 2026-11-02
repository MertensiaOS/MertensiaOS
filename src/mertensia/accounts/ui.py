"""Native desktop account manager; all system calls run outside GTK's thread."""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from mertensia.ui.branding import BRANDING, load_brand_style
from .client import AccountsClient, administrator_request, available_languages, current_identity, error_message
from .validation import RequestError, validate_password


def language_name(code):
    if not code:
        return "System default"
    from mertensia.firstboot.regions import regional_choices
    try:
        return regional_choices("locale", [code])[0].title
    except (ValueError, IndexError):
        return code


class AccountForm(Adw.Dialog):
    def __init__(self, title, button, *, destructive=False):
        super().__init__(title=title, content_width=480, content_height=560)
        self.fields = {}
        self.password_fields = []
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel = Gtk.Button(label="Cancel")
        self.cancel.connect("clicked", lambda *_: self.close())
        self.submit = Gtk.Button(label=button, css_classes=["destructive-action" if destructive else "suggested-action"])
        header.pack_start(self.cancel)
        header.pack_end(self.submit)
        toolbar.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()
        self.page.add(self.group)
        self.message = Gtk.Label(wrap=True, xalign=0, visible=False, css_classes=["error"])
        messages = Adw.PreferencesGroup()
        messages.add(self.message)
        self.page.add(messages)
        toolbar.set_content(self.page)
        self.set_child(toolbar)
        self.connect("closed", self._clear_passwords)

    def entry(self, key, title, *, password=False, text=""):
        row = Adw.PasswordEntryRow(title=title) if password else Adw.EntryRow(title=title)
        row.set_text(text)
        self.fields[key] = row
        if password:
            self.password_fields.append(row)
        self.group.add(row)
        return row

    def error(self, message):
        self.message.set_text(message)
        self.message.set_visible(True)

    def set_busy(self, busy):
        self.set_can_close(not busy)
        self.cancel.set_sensitive(not busy)
        self.submit.set_sensitive(not busy)
        self.page.set_sensitive(not busy)
        if busy:
            self.message.set_visible(False)

    def _clear_passwords(self, *_args):
        for entry in self.password_fields:
            entry.set_text("")


class AccountsWindow(Adw.ApplicationWindow):
    def __init__(self, app, *, client_factory=AccountsClient, admin_request=administrator_request,
                 identity_factory=current_identity, languages_factory=available_languages):
        super().__init__(application=app, title="Mertensia Accounts", default_width=780, default_height=720)
        self.client_factory = client_factory
        self.admin_request = admin_request
        self.identity_factory = identity_factory
        self.languages_factory = languages_factory
        self.identity = identity_factory()
        self.accounts = []
        self.languages = []
        self.selected = None
        self.busy = False
        self.pulse_source = 0
        self.mutating = False
        self.closed = False
        self.pending_add = False
        self.loaded = False
        self.active_form = None
        self.set_icon_name("org.mertensia.Accounts")
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(BRANDING / "icons"))
        self.style = Adw.StyleManager.get_default()
        self.provider = None
        self._style_changed()
        self.style.connect("notify::dark", self._style_changed)
        self.style.connect("notify::high-contrast", self._style_changed)
        self.connect("close-request", self._close_requested)
        self.toasts = Adw.ToastOverlay()
        self.navigation = Adw.NavigationView()
        self.navigation.connect("popped", lambda *_: setattr(self, "selected", None))
        self.toasts.set_child(self.navigation)
        self.set_content(self.toasts)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh accounts")
        self.refresh_button.connect("clicked", self.refresh)
        self.add_button = Gtk.Button(label="Add User", sensitive=False, css_classes=["suggested-action"])
        self.add_button.connect("clicked", self.show_create)
        header.pack_start(self.refresh_button)
        header.pack_end(self.add_button)
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.progress = Gtk.ProgressBar(pulse_step=0.12, visible=False)
        box.append(self.progress)
        self.overview = Adw.PreferencesPage(title="Accounts")
        self.status = Adw.PreferencesGroup()
        self.account_list = Adw.PreferencesGroup(title="Users on this computer",
            description="Each person has a separate encrypted home.")
        self.overview.add(self.status)
        self.overview.add(self.account_list)
        box.append(self.overview)
        self.overview.set_vexpand(True)
        toolbar.set_content(box)
        self.home_page = Adw.NavigationPage(child=toolbar, title="Accounts", tag="accounts")
        self.navigation.add(self.home_page)
        self.refresh()

    def _style_changed(self, *_args):
        if self.provider:
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.provider)
        self.provider = load_brand_style(self.get_display(), self.style.get_dark(),
                                         self.style.get_high_contrast(), stylesheets=())

    def _close_requested(self, *_args):
        if self.mutating:
            self.toast("Wait for the account change to finish.")
            return True
        self.closed = True
        return False

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=message, timeout=5))

    def _run(self, work, done, *, form=None, mutation=True):
        if self.busy:
            return
        self.busy = True
        self.mutating = mutation
        self.navigation.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.pulse()
        self.pulse_source = GLib.timeout_add(150, self._pulse)
        if form:
            form.set_busy(True)

        def finish(value, error):
            self.busy = self.mutating = False
            if self.pulse_source:
                GLib.source_remove(self.pulse_source)
                self.pulse_source = 0
            if self.closed:
                return GLib.SOURCE_REMOVE
            self.navigation.set_sensitive(True)
            self.progress.set_visible(False)
            if form:
                form.set_busy(False)
            if error:
                if form:
                    form.error(error_message(error))
                else:
                    self.toast(error_message(error))
                    if not self.loaded:
                        self.account_list.set_description("Accounts could not be loaded. Use Refresh to try again.")
            else:
                done(value)
            return GLib.SOURCE_REMOVE

        def worker():
            try:
                value = work()
            except Exception as error:
                GLib.idle_add(finish, None, error)
            else:
                GLib.idle_add(finish, value, None)

        threading.Thread(target=worker, daemon=True).start()

    def _pulse(self):
        self.progress.pulse()
        return GLib.SOURCE_CONTINUE

    def refresh(self, *_args):
        def load():
            return self.client_factory().accounts(), self.languages_factory(), self.identity_factory()
        self._run(load, self._loaded, mutation=False)

    def _loaded(self, data):
        self.accounts, self.languages, self.identity = data
        self.loaded = True
        self.add_button.set_sensitive(self.identity.administrator)
        self.overview.remove(self.account_list)
        self.overview.remove(self.status)
        self.status = Adw.PreferencesGroup()
        if not self.identity.administrator:
            self.status.set_description("You can change your own profile and password. An administrator manages other accounts.")
        if not any(account.uid == self.identity.uid for account in self.accounts):
            self.status.set_description("Your current account is not managed by systemd-homed. Encrypted accounts are listed below.")
        self.account_list = Adw.PreferencesGroup(title="Users on this computer",
            description="Each person has a separate encrypted home." if self.accounts else "No encrypted user accounts were found.")
        self.overview.add(self.status)
        self.overview.add(self.account_list)
        for account in self.accounts:
            row = Adw.ActionRow(title=account.name, subtitle=self._subtitle(account), activatable=True, use_markup=False)
            row.add_prefix(self._avatar(account, 48))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _row, username=account.username: self.show_profile(username))
            self.account_list.add(row)
        if self.selected:
            username = self.selected
            self.navigation.pop_to_tag("accounts")
            if any(account.username == username for account in self.accounts):
                self.show_profile(username)
            else:
                self.selected = None
        if self.pending_add:
            self.pending_add = False
            self.show_create()

    def _subtitle(self, account):
        parts = [account.username, "Administrator" if account.administrator else "Standard user"]
        if account.uid == self.identity.uid:
            parts.append("You")
        if account.locked:
            parts.append("Account disabled")
        elif account.signed_in:
            parts.append("Signed in")
        return " · ".join(parts)

    @staticmethod
    def _avatar(account, size):
        avatar = Adw.Avatar(size=size, text=account.name, show_initials=True)
        if account.avatar and Path(account.avatar).is_file():
            try:
                avatar.set_custom_image(Gdk.Texture.new_from_filename(account.avatar))
            except GLib.Error:
                pass
        return avatar

    def _row(self, group, title, subtitle="", button=None, callback=None, *, enabled=True, destructive=False):
        row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False, subtitle_lines=0)
        if button:
            action = Gtk.Button(label=button, valign=Gtk.Align.CENTER,
                                css_classes=["destructive-action"] if destructive else [])
            action.set_sensitive(enabled)
            action.connect("clicked", callback)
            row.add_suffix(action)
        group.add(row)
        return row

    def show_profile(self, username):
        account = next(account for account in self.accounts if account.username == username)
        self.selected = username
        own = account.uid == self.identity.uid
        editable = own or self.identity.administrator
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        appearance = Adw.PreferencesGroup()
        portrait = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_top=12, margin_bottom=12, halign=Gtk.Align.CENTER)
        portrait.append(self._avatar(account, 96))
        photo = Gtk.Button(label="Change Picture", sensitive=editable)
        photo.connect("clicked", lambda *_: self.choose_avatar(account))
        portrait.append(photo)
        appearance.add(portrait)
        page.add(appearance)
        profile = Adw.PreferencesGroup(title="Profile", description=self._subtitle(account))
        name = Adw.EntryRow(title="Full name", text=account.name, show_apply_button=True, sensitive=editable)
        name.connect("apply", lambda entry: self._profile_change(account, "name", entry.get_text()))
        profile.add(name)
        self._row(profile, "Username", account.username)
        self._row(profile, "Language", language_name(account.language), "Change…",
                  lambda *_: self.choose_language(account), enabled=editable)
        self._row(profile, "Password", "Protects your sign-in and encrypted personal files." if own else
                  "The account holder must sign in to change their password.",
                  "Change…", lambda *_: self.show_password(account), enabled=own)
        page.add(profile)
        administration = Adw.PreferencesGroup(title="Account permissions")
        self._row(administration, "Account type", "Administrator" if account.administrator else "Standard user",
                  "Change…", lambda *_: self.change_role(account), enabled=self.identity.administrator)
        self._row(administration, "Sign-in", "Disabled" if account.locked else "Enabled",
                  "Enable" if account.locked else "Disable…", lambda *_: self.change_locked(account),
                  enabled=self.identity.administrator and not own)
        self._row(administration, "Automatic login", "A password is required to unlock an encrypted home.")
        page.add(administration)
        removal = Adw.PreferencesGroup(title="Remove account", description=
            "The user must sign out first. Removing an account permanently deletes its encrypted home and personal files.")
        self._row(removal, "Delete this account and its files", button="Remove User…",
                  callback=lambda *_: self.show_remove(account),
                  enabled=self.identity.administrator and not own and not account.signed_in, destructive=True)
        page.add(removal)
        toolbar.set_content(page)
        navigation_page = Adw.NavigationPage(child=toolbar, title=account.name)
        self.navigation.push(navigation_page)

    def _changed(self, message, form=None):
        if form:
            form.close()
        self.toast(message)
        self.refresh()

    def _profile_change(self, account, field, value):
        self._run(lambda: self.client_factory().set_profile(account.username, field, value),
                  lambda _: self._changed("Profile updated."))

    def _present(self, form):
        self.active_form = form
        form.connect("closed", lambda *_: setattr(self, "active_form", None))
        form.present(self)
        return form

    def show_password(self, account):
        if account.uid != self.identity.uid:
            return
        form = AccountForm("Change Password", "Change")
        form.group.set_description("Enter your current password and choose a new one with at least 8 characters.")
        form.entry("current", "Current password", password=True)
        form.entry("password", "New password", password=True)
        form.entry("confirm", "Confirm new password", password=True)
        keyring = Adw.PreferencesGroup(description="Your login keyring will be updated when available. "
                                      "If a saved-password keyring stays locked, unlock it with your previous password.")
        form.page.add(keyring)

        def submit(*_args):
            values = {key: entry.get_text() for key, entry in form.fields.items()}
            try:
                if not values["current"]:
                    raise RequestError("Enter your current password.")
                validate_password(values["password"])
                if values["password"] != values["confirm"]:
                    raise RequestError("The new passwords do not match.")
            except RequestError as error:
                form.error(str(error))
                return
            def work():
                try:
                    return self.client_factory().change_password(account.username, values["current"], values["password"])
                finally:
                    values.clear()
            def done(warning):
                for entry in form.fields.values():
                    entry.set_text("")
                self._changed("Password changed. Use your new password to sign in.", form)
                if warning:
                    dialog = Adw.AlertDialog(heading="Password changed", body=warning)
                    dialog.add_response("close", "Close")
                    dialog.present(self)
            self._run(work, done, form=form)
        form.submit.connect("clicked", submit)
        self._present(form)

    def show_create(self, *_args):
        if not self.loaded:
            self.pending_add = True
            return
        if not self.identity.administrator or self.busy:
            return
        form = AccountForm("Add User", "Create Account")
        form.group.set_description("Their password protects their encrypted home. Keep it somewhere safe; administrators cannot recover it.")
        form.entry("real_name", "Full name")
        form.entry("username", "Username")
        form.entry("password", "Password", password=True)
        form.entry("confirm", "Confirm password", password=True)
        administrator = Adw.SwitchRow(title="Administrator", subtitle="Allow managing users and system settings.")
        form.group.add(administrator)

        def submit(*_args):
            from mertensia.firstboot.ui import account_error
            values = {key: entry.get_text() for key, entry in form.fields.items()}
            error = account_error(values)
            if error:
                form.error(error[1])
                form.fields[error[0]].grab_focus()
                return
            values.pop("confirm")
            values.update(admin=administrator.get_active(), firstboot=False)
            def work():
                try:
                    return self.admin_request(values)
                finally:
                    values.clear()
            self._run(work, lambda _: self._changed("Account created. They can now sign in.", form), form=form)
        form.submit.connect("clicked", submit)
        self._present(form)

    def choose_language(self, account):
        form = AccountForm("Language", "Save")
        codes = [""] + self.languages
        if account.language and account.language not in codes:
            codes.append(account.language)
        model = Gtk.StringList.new([language_name(code) for code in codes])
        language = Adw.ComboRow(title="Language", model=model, selected=codes.index(account.language))
        form.group.add(language)
        form.group.set_description("The change takes effect the next time this user signs in.")
        def submit(*_args):
            value = codes[language.get_selected()]
            self._run(lambda: self.client_factory().set_profile(account.username, "language", value),
                      lambda _: self._changed("Language updated.", form), form=form)
        form.submit.connect("clicked", submit)
        self._present(form)

    def choose_avatar(self, account):
        dialog = Gtk.FileDialog(title="Choose a profile picture")
        images = Gtk.FileFilter(name="Images")
        images.add_pixbuf_formats()
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        def chosen(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path()
            if not path:
                self.toast("Choose a picture stored on this computer.")
                return
            def work():
                if Path(path).stat().st_size > 10 * 1024 * 1024:
                    raise RequestError("Choose a picture smaller than 10 MB.")
                # Decode and resize as the caller, never in the root helper.
                image = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 256, 256, True)
                with tempfile.TemporaryDirectory(prefix="mertensia-avatar-") as directory:
                    avatar = str(Path(directory) / "avatar.png")
                    image.savev(avatar, "png", [], [])
                    self.client_factory().set_profile(account.username, "avatar", avatar)
            self._run(work, lambda _: self._changed("Profile picture updated."))
        dialog.open(self, None, chosen)

    def _confirm_admin(self, account, heading, body, operation, enabled, label):
        if not self.identity.administrator or self.busy:
            return
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", label)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        def response(_dialog, response):
            if response == "apply":
                self._run(lambda: self.admin_request({"operation": operation, "username": account.username,
                                                       "enabled": enabled}),
                          lambda _: self._changed("Account updated. Sign out and back in for changed permissions."))
        dialog.connect("response", response)
        dialog.present(self)

    def change_role(self, account):
        self._confirm_admin(account,
            f"Make {account.name} a standard user?" if account.administrator else f"Make {account.name} an administrator?",
            "Standard users manage their own account. Administrators can also add, change and remove other accounts.",
            "set-admin", not account.administrator, "Make Standard" if account.administrator else "Make Administrator")

    def change_locked(self, account):
        self._confirm_admin(account,
            f"Enable {account.name}'s account?" if account.locked else f"Disable {account.name}'s account?",
            "Disabling prevents future sign-ins and keeps their files. It does not end an existing session.",
            "set-locked", not account.locked, "Enable Account" if account.locked else "Disable Account")

    def show_remove(self, account):
        if not self.identity.administrator or account.uid == self.identity.uid or account.signed_in:
            return
        form = AccountForm("Remove User", "Delete User", destructive=True)
        form.group.set_description(f"Permanently delete {account.name}'s encrypted home and all their personal files. "
                                  f"This cannot be undone. Type {account.username} to confirm.")
        entry = form.entry("confirmation", "Username")
        form.submit.set_sensitive(False)
        entry.connect("changed", lambda row: form.submit.set_sensitive(row.get_text() == account.username))
        def submit(*_args):
            confirmation = entry.get_text()
            self._run(lambda: self.admin_request({"operation": "remove", "username": account.username,
                                                  "confirm_username": confirmation}),
                      lambda _: self._changed("Account and encrypted home deleted.", form), form=form)
        form.submit.connect("clicked", submit)
        self._present(form)


class AccountsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.mertensia.Accounts", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def do_activate(self):
        window = self.get_active_window() or AccountsWindow(self)
        window.present()

    def do_command_line(self, command_line):
        self.activate()
        if "--add-user" in command_line.get_arguments():
            self.get_active_window().show_create()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage encrypted MertensiaOS user accounts")
    parser.add_argument("--add-user", action="store_true", help="Open the account creation form")
    args = parser.parse_args(argv)
    return AccountsApp().run(["mertensia-accounts"] + (["--add-user"] if args.add_user else []))
