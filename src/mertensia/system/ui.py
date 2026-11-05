"""Shared desktop shell for OS updates and encrypted accounts."""

import argparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from mertensia.accounts.ui import AccountsView
from mertensia.ui.branding import BRANDING, load_brand_style
from mertensia.updates.ui import UpdatesView


class SystemWindow(Adw.ApplicationWindow):
    def __init__(self, app, *, accounts_factory=AccountsView, updates_factory=UpdatesView):
        super().__init__(application=app, title="Mertensia System", default_width=780, default_height=760)
        self.set_icon_name("org.mertensia.System")
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(BRANDING / "icons"))
        self.style = Adw.StyleManager.get_default()
        self.provider = None
        self._style_changed()
        self.style_handlers = [self.style.connect("notify::" + key, self._style_changed)
                               for key in ("dark", "high-contrast")]
        self.stack = Adw.ViewStack()
        self.updates = updates_factory()
        self.accounts = accounts_factory()
        self.stack.add_titled_with_icon(self.updates, "updates", "OS Updates", "software-update-available-symbolic")
        self.stack.add_titled_with_icon(self.accounts, "accounts", "Accounts", "system-users-symbolic")
        for view in (self.updates, self.accounts):
            view.header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.NARROW))
        self.set_content(self.stack)
        self.connect("close-request", self._close_requested)

    def _style_changed(self, *_args):
        if self.provider:
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.provider)
        self.provider = load_brand_style(self.get_display(), self.style.get_dark(),
                                         self.style.get_high_contrast(), stylesheets=())

    def show_page(self, page, *, add_user=False):
        if self.accounts.mutating or self.accounts.active_form:
            self.accounts.toast("Finish the open account dialog before changing views.")
            return
        self.stack.set_visible_child_name("accounts" if add_user else page)
        if add_user:
            self.accounts.show_create()

    def _close_requested(self, *_args):
        if self.accounts._close_requested():
            return True
        self.updates.close()
        for handler in self.style_handlers:
            self.style.disconnect(handler)
        if self.provider:
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.provider)
            self.provider = None
        return False


def argument_parser():
    parser = argparse.ArgumentParser(description="Manage MertensiaOS updates and encrypted accounts")
    parser.add_argument("--page", choices=("updates", "accounts"), default="updates")
    parser.add_argument("--add-user", action="store_true", help="Open the account creation form")
    return parser


class SystemApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.mertensia.System", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def do_activate(self):
        window = self.get_active_window() or SystemWindow(self)
        window.present()

    def do_command_line(self, command_line):
        try:
            args = argument_parser().parse_args(command_line.get_arguments()[1:])
        except SystemExit as error:
            return int(error.code)
        self.activate()
        self.get_active_window().show_page(args.page, add_user=args.add_user)
        return 0


def main(argv=None):
    args = argument_parser().parse_args(argv)
    arguments = ["mertensia-system", "--page", args.page] + (["--add-user"] if args.add_user else [])
    return SystemApp().run(arguments)
