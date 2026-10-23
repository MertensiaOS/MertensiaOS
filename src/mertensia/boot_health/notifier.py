"""Notify each desktop user once about a confirmed automatic OS recovery."""

import json
import os
from pathlib import Path
import pwd

from .backend import EVENT, atomic_json


MESSAGE = "MertensiaOS restored the previous OS version because an update could not start correctly."


def pending_event(event_path, seen_path):
    try:
        event = json.loads(event_path.read_text())
        if (event.get("version") != 1 or not isinstance(event.get("event_id"), str)
                or not event["event_id"].startswith("rollback-") or not event.get("restored_image_digest")):
            return None
        try:
            seen = json.loads(seen_path.read_text())
        except (OSError, ValueError):
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        return event if seen.get("event_id") != event["event_id"] else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def deliver(event_path, seen_path, send):
    event = pending_event(event_path, seen_path)
    if event is None:
        return False
    send(MESSAGE)
    atomic_json(seen_path, {"event_id": event["event_id"]}, mode=0o600)
    return True


def send_notification(message):
    from gi.repository import Gio, GLib

    # A synchronous reply confirms the daemon accepted this notification. Keep
    # the event pending when the session bus or notification service is absent.
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    connection.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                         "org.freedesktop.Notifications", "Notify",
                         GLib.Variant("(susssasa{sv}i)", (
                             "MertensiaOS", 0, "dialog-warning-symbolic", "OS update recovery", message,
                             [], {"desktop-entry": GLib.Variant("s", "org.mertensia.BootHealth")}, -1)),
                         GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)


def main():
    user = pwd.getpwuid(os.getuid())
    if (user.pw_name == "mertensia-setup" or os.getuid() < 1000
            or Path("/usr/bin/mertensia-installer").exists()):
        return 0
    from gi.repository import Gio, GLib

    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    seen = state / "mertensia/boot-health-notification.json"
    app = Gio.Application(application_id="org.mertensia.BootHealth", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def attempt(*_):
        try:
            deliver(EVENT, seen, send_notification)
        except (OSError, ValueError, GLib.Error):
            pass  # The periodic retry also covers a late notification daemon.
        return GLib.SOURCE_CONTINUE

    def activate(application):
        application.hold()
        # The directory is provisioned before graphical boot. Watch the directory
        # because the root collector replaces the event file atomically.
        try:
            monitor = Gio.File.new_for_path(str(EVENT.parent)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", attempt)
            application.event_monitor = monitor
        except GLib.Error:
            pass
        GLib.timeout_add_seconds(30, attempt)
        attempt()

    app.connect("activate", activate)
    return app.run([])
