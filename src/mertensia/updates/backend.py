"""Stage bootc updates without restarting, after local boot acceptance."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess

from mertensia.boot_health.backend import EVENT, atomic_json, installed_system


STATE = Path("/var/lib/mertensia/updates")
STATUS = STATE / "status.json"
ACKNOWLEDGEMENT = STATE / "acknowledgement.json"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


class UpdateError(RuntimeError):
    pass


def command(argv, timeout=15):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"{Path(argv[0]).name} unavailable or timed out") from error
    if result.returncode:
        # Full bootc diagnostics remain in its service journal, rather than in
        # public desktop state. Do not publish registry authentication details.
        raise UpdateError(f"{Path(argv[0]).name} exited with status {result.returncode}")
    return result.stdout


def read_record(path):
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(record, dict):
        raise UpdateError(f"Invalid {path.name}")
    return record


def recovery_pending():
    event = read_record(EVENT)
    if not event and not EVENT.exists():
        return None
    if (event.get("version") != 1 or not isinstance(event.get("event_id"), str)
            or not event["event_id"].startswith("rollback-") or not event.get("restored_image_digest")):
        raise UpdateError("Invalid recovery event; automatic OS updates remain paused")
    acknowledged = read_record(ACKNOWLEDGEMENT).get("event_id")
    return event["event_id"] if event["event_id"] != acknowledged else None


def bootc_status():
    data = json.loads(command(["bootc", "status", "--json"]))
    status = data["status"]
    if not isinstance(status, dict) or not isinstance(status.get("booted"), dict):
        raise UpdateError("No booted bootc deployment")
    return status


def publish(status, reason=None):
    staged = status.get("staged")
    digest = staged.get("image", {}).get("imageDigest") if staged else None
    record = {"version": 1, "boot_id": BOOT_ID.read_text().strip(),
              "staged_digest": digest, "recovery_pending": recovery_pending(), "reason": reason}
    atomic_json(STATUS, record)
    return record


def healthy_boot():
    for unit in ("greenboot-healthcheck.service", "mertensia-boot-health-status.service"):
        properties = command(["systemctl", "show", unit, "--property=ActiveState,Result,ExecMainStartTimestampMonotonic"])
        state = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        if (state.get("ActiveState") != "active" or state.get("Result") != "success"
                or state.get("ExecMainStartTimestampMonotonic", "0") == "0"):
            return False
    return True


def unmetered_network():
    # NetworkManager's global state is CONNECTED_GLOBAL=70.
    # NO=2 and GUESS_NO=4 are unmetered; UNKNOWN, YES and GUESS_YES defer.
    for property_name, expected in (("State", {"u 70"}), ("Metered", {"u 2", "u 4"})):
        output = command(["busctl", "--system", "get-property", "org.freedesktop.NetworkManager",
                          "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager", property_name])
        if output.strip() not in expected:
            return False
    return True


def skip_reason(status):
    if recovery_pending():
        return "Automatic OS updates are paused after recovery; run sudo mertensia-update resume"
    if status.get("staged"):
        return "An OS deployment is already staged"
    if status.get("rollbackQueued"):
        return "A manual rollback is queued"
    booted = status["booted"]
    image = booted.get("image", {}).get("image", {})
    reference = image.get("image", "")
    if image.get("transport") != "registry" or not reference or "@" in reference:
        return "The installed image does not follow a registry tag"
    if image.get("signature") != "containerPolicy":
        return "The installed image does not enforce the container signature policy"
    if booted.get("incompatible"):
        return "The deployment contains local changes incompatible with bootc"
    if not healthy_boot():
        return "Boot health and recovery collection have not completed successfully"
    if not unmetered_network():
        return "Waiting for an unmetered internet connection"
    return None


@contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (STATE / "lock").open("a") as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def stage():
    status = bootc_status()
    reason = skip_reason(status)
    if reason:
        print(reason, flush=True)
        return publish(status, reason)
    # Inherit stdout/stderr so bootc's diagnostics go directly to the journal.
    # Never use --apply: the user's normal restart activates this deployment.
    try:
        result = subprocess.run(["bootc", "upgrade"], timeout=3600, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        publish(bootc_status(), "OS update download was interrupted or timed out")
        raise UpdateError("OS update download was interrupted or timed out") from error
    if result.returncode:
        publish(bootc_status(), "OS update staging failed; see the update service journal")
        raise UpdateError(f"bootc upgrade exited with status {result.returncode}")
    return publish(bootc_status())


def resume():
    # Resumption is an explicit acknowledgement, not an upgrade or a reboot.
    if not healthy_boot():
        raise UpdateError("Wait for successful boot health and recovery collection before resuming")
    event_id = recovery_pending()
    if event_id:
        atomic_json(ACKNOWLEDGEMENT, {"event_id": event_id}, mode=0o600)
    return publish(bootc_status())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "status", "resume"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            if args.operation != "status":
                raise UpdateError("OS update operations require root")
            print(json.dumps(read_record(STATUS), sort_keys=True))
            return 0
        if not installed_system():
            print("Skipping OS updates outside the installed system")
            return 0
        with locked():
            if args.operation == "stage":
                record = stage()
            elif args.operation == "resume":
                record = resume()
            else:
                record = publish(bootc_status())
        print(json.dumps(record, sort_keys=True))
        return 0
    except (UpdateError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"MertensiaOS updates: {error}", flush=True)
        return 1
