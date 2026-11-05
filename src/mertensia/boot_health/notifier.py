"""Notify desktop users about staged OS updates and confirmed recovery."""

import json
import os
from pathlib import Path
import pwd

from .backend import EVENT, atomic_json
from mertensia.updates.backend import BOOT_ID, STATUS


MESSAGE = "MertensiaOS restored the previous OS version because an update could not start correctly."
UPDATE_MESSAGE = "An OS update is ready. Restart when convenient."
PAUSED_MESSAGE = "Automatic OS updates are paused. Open OS Updates in Mertensia System to review and resume them."


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
        message += " Automatic OS updates pause after recovery. Open OS Updates in Mertensia System to review and resume them."
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


def send_notification(message, notification_ids=None):
    from gi.repository import Gio, GLib

    # A synchronous reply confirms the daemon accepted this notification. Keep
    # the event pending when the session bus or notification service is absent.
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    reply = connection.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                         "org.freedesktop.Notifications", "Notify",
                         GLib.Variant("(susssasa{sv}i)", (
                             "MertensiaOS", 0,
                             "software-update-available-symbolic" if message == UPDATE_MESSAGE else "dialog-warning-symbolic",
                             "OS update ready" if message == UPDATE_MESSAGE else "OS update recovery", message,
                             ["default", "Open OS Updates"],
                             {"desktop-entry": GLib.Variant("s", "org.mertensia.System")}, -1)),
                         GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
    if notification_ids is not None:
        notification_ids.add(reply.unpack()[0])


def notification_action(notification_ids, parameters):
    """Only activate actions for notifications sent by this notifier instance."""
    from gi.repository import Gio

    notification_id, action = parameters.unpack()
    if notification_id not in notification_ids or action != "default":
        return
    Gio.Subprocess.new(["/usr/bin/mertensia-system", "--page", "updates"], Gio.SubprocessFlags.NONE)
    notification_ids.discard(notification_id)


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
    notification_ids = set()

    def send(message):
        send_notification(message, notification_ids)

    def attempt(*_):
        try:
            deliver(EVENT, seen, send)
        except (OSError, ValueError, GLib.Error):
            pass  # The periodic retry also covers a late notification daemon.
        try:
            deliver_update(STATUS, update_seen, send)
        except (OSError, ValueError, GLib.Error):
            pass
        return GLib.SOURCE_CONTINUE

    def activate(application):
        if getattr(application, "event_monitors", None) is not None:
            return
        application.hold()
        application.notification_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def action(_connection, _sender, _path, _interface, _signal, parameters, _data):
            try:
                notification_action(notification_ids, parameters)
            except GLib.Error:
                pass

        application.notification_connection.signal_subscribe(
            "org.freedesktop.Notifications", "org.freedesktop.Notifications", "ActionInvoked",
            "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE, action, None)
        application.notification_connection.signal_subscribe(
            "org.freedesktop.Notifications", "org.freedesktop.Notifications", "NotificationClosed",
            "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE,
            lambda _c, _s, _p, _i, _n, parameters, _d: notification_ids.discard(parameters.unpack()[0]), None)
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
