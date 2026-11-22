"""OS update view; IPC runs in workers and downloads belong to systemd."""

from datetime import datetime
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk
from . import client


def version_label(deployment):
    deployment = deployment or {}
    return deployment.get("version") or deployment.get("digest") or "Not available yet"


def time_label(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return "Not recorded"


class UpdateBanner(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=20, margin_bottom=12, margin_start=12, margin_end=12)
        self.icon = Gtk.Image(icon_name="software-update-available-symbolic", pixel_size=48)
        self.heading = Gtk.Label(label="Loading update status", wrap=True, justify=Gtk.Justification.CENTER,
                                 css_classes=["title-1"])
        self.description = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"])
        for widget in (self.icon, self.heading, self.description):
            self.append(widget)

    def set_title(self, title):
        self.heading.set_text(title)

    def get_title(self):
        return self.heading.get_text()

    def set_description(self, description):
        self.description.set_text(description)

    def set_icon_name(self, icon):
        self.icon.set_from_icon_name(icon)


class UpdatesView(Adw.Bin):
    def __init__(self, *, load=client.snapshot, request=client.administrator_request, restart=None):
        super().__init__()
        self.load = load
        self.request = request
        self.restart = restart or self._restart
        self.busy = self.closed = False
        self.state = None
        self.updating_switch = False
        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh update status")
        self.refresh_button.connect("clicked", self.refresh)
        self.header.pack_start(self.refresh_button)
        toolbar.add_top_bar(self.header)
        self.page = Adw.PreferencesPage(title="OS Updates", description="OS updates include Brave Origin and take effect after a restart.")
        toolbar.set_content(self.page)
        self.toasts.set_child(toolbar)
        self.set_child(self.toasts)

        group = Adw.PreferencesGroup()
        self.banner = UpdateBanner()
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, visible=False)
        self.banner.append(self.spinner)
        group.add(self.banner)
        self.page.add(group)

        actions = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_bottom=12)
        self.download_button = Gtk.Button(label="Check and Download", css_classes=["suggested-action"], sensitive=False)
        self.download_button.connect("clicked", lambda *_: self.operate("download"))
        self.resume_button = Gtk.Button(label="Resume Updates…", visible=False)
        self.resume_button.connect("clicked", self.confirm_resume)
        self.restart_button = Gtk.Button(label="Restart…", visible=False)
        self.restart_button.connect("clicked", lambda *_: self._run(self.restart, lambda _: None))
        for button in (self.download_button, self.resume_button, self.restart_button):
            actions.append(button)
        group.add(actions)

        versions = Adw.PreferencesGroup(title="This system")
        self.current_row = Adw.ActionRow(title="Current version", use_markup=False)
        self.staged_row = Adw.ActionRow(title="Next restart", use_markup=False, visible=False)
        self.checked_row = Adw.ActionRow(title="Last successful check", use_markup=False)
        for row in (self.current_row, self.staged_row, self.checked_row):
            row.set_subtitle_selectable(True)
            versions.add(row)
        self.page.add(versions)

        settings = Adw.PreferencesGroup(title="Automatic updates",
            description="Administrator authentication is required to change OS updates.")
        self.automatic = Adw.SwitchRow(title="Download OS updates automatically",
            subtitle="Check daily on unmetered connections. You choose when to restart.", sensitive=False)
        self.automatic.connect("notify::active", self._automatic_changed)
        settings.add(self.automatic)
        self.timer_note = Adw.ActionRow(title="", use_markup=False, visible=False)
        settings.add(self.timer_note)
        self.page.add(settings)
        self.reason_row = Adw.ActionRow(title="Last update attempt", use_markup=False, visible=False)
        diagnostics = Adw.PreferencesGroup()
        details = Adw.ExpanderRow(title="Update details", subtitle="Last attempt and troubleshooting")
        details.add_row(self.reason_row)
        row = Adw.ActionRow(title="Update service journal", subtitle="journalctl -u bootc-fetch-apply-updates.service",
                            subtitle_selectable=True, use_markup=False)
        details.add_row(row)
        diagnostics.add(details)
        self.page.add(diagnostics)
        apps = Adw.PreferencesGroup(description="Application updates and permissions are managed separately in Software.")
        software = Adw.ButtonRow(title="Open Software", start_icon_name="system-software-install-symbolic")
        software.connect("activated", self._open_software)
        apps.add(software)
        self.page.add(apps)
        self.refresh()
        self.poll_source = GLib.timeout_add_seconds(5, self.refresh)

    def close(self):
        self.closed = True
        GLib.source_remove(self.poll_source)

    def _run(self, work, done, *, report_error=True):
        if self.busy or self.closed:
            return
        self.busy = True
        self._sensitivity()

        def finish(value, error):
            self.busy = False
            if self.closed:
                return GLib.SOURCE_REMOVE
            if error:
                if report_error:
                    self.toasts.add_toast(Adw.Toast(title=str(error), timeout=8))
                if self.state is None:
                    self.banner.set_title("Status unavailable")
                    self.banner.set_description("Could not read OS update status. Use Refresh to try again.")
                else:
                    # Never leave actionable controls based on a stale successful read.
                    self.state = None
                    self.banner.set_title("Refresh update status")
                    self.banner.set_description(str(error))
            else:
                done(value)
            self._sensitivity()
            return GLib.SOURCE_REMOVE

        def worker():
            try:
                value = work()
            except Exception as error:
                GLib.idle_add(finish, None, error)
            else:
                GLib.idle_add(finish, value, None)

        threading.Thread(target=worker, daemon=True).start()

    def _sensitivity(self):
        state = self.state or {}
        record = state.get("record", {})
        enabled = not self.busy and state.get("supported", False)
        self.refresh_button.set_sensitive(not self.busy)
        self.automatic.set_sensitive(enabled)
        self.download_button.set_sensitive(bool(enabled and state.get("current") and not state.get("running")
            and not any(record.get(key) for key in ("recovery_pending", "rollback_queued", "staged_digest"))))
        self.resume_button.set_sensitive(bool(enabled and not state.get("running")))
        self.restart_button.set_sensitive(bool(not self.busy and state.get("current") and not state.get("running")))
        spinning = self.busy or state.get("running", False)
        self.spinner.set_visible(spinning)
        self.spinner.set_spinning(spinning)

    def refresh(self, *_args):
        self._run(self.load, self._loaded, report_error=bool(_args))
        return GLib.SOURCE_CONTINUE

    def _loaded(self, state):
        self.state = state
        record = state["record"]
        title, description, icon = client.presentation(state)
        self.banner.set_title(title)
        self.banner.set_description(description)
        self.banner.set_icon_name(icon)
        self.current_row.set_subtitle(version_label(record.get("booted")))
        self.staged_row.set_visible(bool(record.get("staged_digest") or record.get("rollback_queued")))
        self.staged_row.set_subtitle("Previous OS version" if record.get("rollback_queued") else version_label(record.get("staged")))
        self.checked_row.set_subtitle(time_label(record.get("last_successful_check")))
        self.resume_button.set_visible(bool(record.get("recovery_pending")))
        self.restart_button.set_visible(bool(record.get("staged_digest") or record.get("rollback_queued")))
        self.updating_switch = True
        self.automatic.set_active(state["timer_enabled"])
        self.updating_switch = False
        self.timer_note.set_visible(not state["timer_active"] or not state["timer_enabled"])
        self.timer_note.set_title("Automatic checks are not running" if state["timer_enabled"] else "Automatic downloads are off")
        self.timer_note.set_subtitle("Turning this off does not cancel a running download or remove an already staged update.")
        self.reason_row.set_visible(bool(record.get("last_attempt") or record.get("reason")))
        self.reason_row.set_subtitle(" · ".join(filter(None, (time_label(record.get("last_attempt")), record.get("reason")))))

    def operate(self, operation):
        def work():
            self.request(operation)
            return self.load()
        self._run(work, self._loaded)

    def _automatic_changed(self, row, *_args):
        if self.updating_switch or self.busy or self.state is None:
            return
        desired = row.get_active()
        self.updating_switch = True
        row.set_active(self.state["timer_enabled"])
        self.updating_switch = False
        self.operate("enable" if desired else "disable")

    def confirm_resume(self, *_args):
        dialog = Adw.AlertDialog(heading="Resume OS updates?", body=
            "The previous OS version was restored after a boot failure. Review and address the failure first. "
            "Resuming allows the same published image to be downloaded again. It does not download or restart now.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("resume", "Resume Updates")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: self.operate("resume") if response == "resume" else None)
        dialog.present(self)

    @staticmethod
    def _restart():
        # GNOME owns the confirmation dialog, session inhibitors and logout flow.
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager",
                             "org.gnome.SessionManager", "Reboot", None, None,
                             Gio.DBusCallFlags.NONE, 10000, None)

    def _open_software(self, *_args):
        try:
            Gio.Subprocess.new(["/usr/bin/gnome-software", "--mode=updates"], Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
