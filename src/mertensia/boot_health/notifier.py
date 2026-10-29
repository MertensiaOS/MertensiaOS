"""Notify each desktop user once about a confirmed automatic OS recovery."""

import json
import os
from pathlib import Path
import pwd

from .backend import EVENT, atomic_json
from mertensia.updates.backend import BOOT_ID, STATUS


MESSAGE = "MertensiaOS restored the previous OS version because an update could not start correctly."
UPDATE_MESSAGE = "An OS update is ready. Restart when convenient."
PAUSED_MESSAGE = "Automatic OS updates are paused until an administrator runs sudo mertensia-update resume."


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


def deliver(event_path, seen_path, send, status_path=STATUS):
    event = pending_event(event_path, seen_path)
    if event is None:
        return False
    message = MESSAGE
    try:
        status = json.loads(status_path.read_text())
        if status.get("recovery_pending") == event["event_id"]:
            message += " " + PAUSED_MESSAGE
    except (OSError, ValueError, TypeError, AttributeError):
        # The recovery event may precede the boot's update status publication.
        message += " Automatic OS updates pause after recovery; an administrator can resume them with sudo mertensia-update resume."
    send(message)
    atomic_json(seen_path, {"event_id": event["event_id"]}, mode=0o600)
    return True


def deliver_update(status_path, seen_path, send, boot_id=None):
    try:
        status = json.loads(status_path.read_text())
        current_boot = BOOT_ID.read_text().strip() if boot_id is None else boot_id
        if status.get("version") != 1 or status.get("boot_id") != current_boot:
            return False
        digest = status.get("staged_digest")
        try:
            seen = json.loads(seen_path.read_text())
        except (OSError, ValueError):
            seen = {}
        if not digest or status.get("recovery_pending"):
            if seen:
                atomic_json(seen_path, {}, mode=0o600)
            return False
        if isinstance(seen, dict) and seen.get("staged_digest") == digest:
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    send(UPDATE_MESSAGE)
    atomic_json(seen_path, {"staged_digest": digest}, mode=0o600)
    return True


def send_notification(message):
    from gi.repository import Gio, GLib

    # A synchronous reply confirms the daemon accepted this notification. Keep
    # the event pending when the session bus or notification service is absent.
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    connection.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                         "org.freedesktop.Notifications", "Notify",
                         GLib.Variant("(susssasa{sv}i)", (
                             "MertensiaOS", 0,
                             "software-update-available-symbolic" if message == UPDATE_MESSAGE else "dialog-warning-symbolic",
                             "OS update ready" if message == UPDATE_MESSAGE else "OS update recovery", message,
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
    update_seen = state / "mertensia/update-notification.json"
    app = Gio.Application(application_id="org.mertensia.BootHealth", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def attempt(*_):
        try:
            deliver(EVENT, seen, send_notification)
        except (OSError, ValueError, GLib.Error):
            pass  # The periodic retry also covers a late notification daemon.
        try:
            deliver_update(STATUS, update_seen, send_notification)
        except (OSError, ValueError, GLib.Error):
            pass
        return GLib.SOURCE_CONTINUE

    def activate(application):
        application.hold()
        # The directory is provisioned before graphical boot. Watch the directory
        # because the root collector replaces the event file atomically.
        application.event_monitors = []
        for directory in (EVENT.parent, STATUS.parent):
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                monitor.connect("changed", attempt)
                application.event_monitors.append(monitor)
            except GLib.Error:
                pass
        GLib.timeout_add_seconds(30, attempt)
        attempt()

    app.connect("activate", activate)
    return app.run([])
