"""Unprivileged update status and a narrowly scoped pkexec client."""

import json
from pathlib import Path
import subprocess

from .backend import BOOT_ID, STATUS, UpdateError
from .helper import OPERATIONS, SERVICE, TIMER


def unit_properties(unit):
    result = subprocess.run(["/usr/bin/systemctl", "show", unit,
                             "--property=LoadState,ActiveState,SubState,Result,UnitFileState"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise UpdateError("Update service status could not be read. Try refreshing.")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def snapshot():
    try:
        record = json.loads(STATUS.read_text())
    except FileNotFoundError:
        record = {}
    if not isinstance(record, dict) or (record and record.get("version") != 1):
        raise UpdateError("OS update status is invalid. Try refreshing after boot completes.")
    current = bool(record) and record.get("boot_id") == BOOT_ID.read_text().strip()
    timer, service = unit_properties(TIMER), unit_properties(SERVICE)
    supported = (Path("/run/ostree-booted").exists() and not Path("/usr/bin/mertensia-installer").exists()
                 and timer.get("LoadState") == "loaded" and service.get("LoadState") == "loaded")
    return {"record": record if current else {}, "current": current, "supported": supported,
            "timer_enabled": timer.get("UnitFileState") in {"enabled", "enabled-runtime"},
            "timer_active": timer.get("ActiveState") == "active",
            "running": service.get("ActiveState") in {"active", "activating", "reloading", "deactivating"},
            "failed": service.get("ActiveState") == "failed" or service.get("Result") not in (None, "", "success")}


def administrator_request(operation):
    if operation not in OPERATIONS:
        raise UpdateError("Unsupported OS update operation.")
    result = subprocess.run(["/usr/bin/pkexec", "/usr/libexec/mertensia-update-helper", operation],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode == 126:
        raise UpdateError("Authentication was cancelled. You can try again.")
    if result.returncode:
        # The helper emits only public error messages, not bootc diagnostics.
        raise UpdateError(result.stderr.strip() or "Authorization failed. Sign in as an administrator and try again.")


def presentation(state):
    """Keep status wording truthful, including stale files and interrupted downloads."""
    record = state["record"]
    if not state["supported"]:
        return "OS updates unavailable", "Updates are available on an installed MertensiaOS system.", "dialog-information-symbolic"
    if record.get("recovery_pending"):
        return "Updates paused after recovery", "The previous OS version was restored. Review the failure before resuming automatic updates.", "dialog-warning-symbolic"
    if record.get("rollback_queued"):
        return "Rollback ready", "Your next restart will return to the previous OS version.", "edit-undo-symbolic"
    if record.get("staged_digest"):
        return "Ready to restart", "An OS update is ready. Restart when convenient to use the new version.", "system-reboot-symbolic"
    if state["running"]:
        return "Checking and downloading", "Updates will be staged for your next restart. You can close this window.", "folder-download-symbolic"
    if not state["current"]:
        return "Waiting for system status", "Update status has not been published for this boot. Refresh after boot health checks complete.", "dialog-information-symbolic"
    if state["failed"] or record.get("activity") == "failed":
        return "Update attempt failed", "The running OS is unchanged. Check the update service journal for details, then try again.", "dialog-warning-symbolic"
    if record.get("activity") == "downloading":
        return "Update attempt interrupted", "The update service is no longer running. Try checking and downloading again.", "dialog-warning-symbolic"
    if record.get("activity") == "deferred" and record.get("reason"):
        return "Update download deferred", record["reason"], "dialog-information-symbolic"
    return "No update staged", "Check and download to look for a published OS update. Updates take effect after a restart.", "software-update-available-symbolic"
