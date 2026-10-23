"""Offline boot checks and a read-only bridge from Greenboot to the desktop."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


EVENT = Path("/var/lib/mertensia/boot-health/recovery.json")
HEALTH_UNIT = "greenboot-healthcheck.service"
SERVICE_PROPERTIES = (
    "ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainStartTimestampMonotonic",
)


class HealthError(RuntimeError):
    pass


def command(argv, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HealthError("health-check deadline reached")
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=min(5, remaining))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HealthError(f"{argv[0]} unavailable or timed out") from error
    if result.returncode:
        # Never copy command output, account records or home metadata into logs.
        raise HealthError(f"{argv[0]} exited with status {result.returncode}")
    return result.stdout


def installed_system():
    if not Path("/run/ostree-booted").exists() or Path("/usr/bin/mertensia-installer").exists():
        return False
    result = subprocess.run(["systemd-detect-virt", "--container", "--quiet"], timeout=5, check=False)
    if result.returncode not in (0, 1):
        raise HealthError("could not determine whether this is a container")
    return result.returncode == 1


def service_state(unit, deadline):
    output = command(["systemctl", "show", unit, "--property=" + ",".join(SERVICE_PROPERTIES)], deadline)
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def synchronization_complete(state):
    return (state.get("ActiveState") in {"active", "inactive"}
            and state.get("Result") == "success" and state.get("ExecMainStatus") == "0"
            and state.get("ExecMainStartTimestampMonotonic", "0") != "0")


def storage_checks(deadline):
    encrypted = False
    for mount in ("/sysroot", "/"):
        try:
            source = command(["findmnt", "--nofsroot", "-nro", "SOURCE", "--mountpoint", mount], deadline).strip()
            if not source.startswith(("/dev/mapper/", "/dev/dm-")):
                continue
            details = command(["cryptsetup", "status", source], deadline)
            encrypted = any(line.strip().startswith("type:") and "LUKS" in line for line in details.splitlines())
            if encrypted:
                break
        except HealthError:
            continue
    if not encrypted:
        raise HealthError("installed root has no accessible LUKS backing mapping")
    command(["findmnt", "-nro", "TARGET", "--mountpoint", "/boot"], deadline)
    command(["grub2-editenv", "/boot/grub2/grubenv", "list"], deadline)
    try:
        with tempfile.TemporaryFile(dir="/var", prefix="mertensia-boot-health-") as stream:
            stream.write(b"boot-health\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise HealthError("/var is not writable") from error


def desktop_problems(deadline):
    problems = []
    for unit in ("gdm.service", "systemd-homed.service", "accounts-daemon.service", "mertensia-login-users.service"):
        try:
            state = service_state(unit, deadline)
            ready = synchronization_complete(state) if unit == "mertensia-login-users.service" else state.get("ActiveState") == "active"
            if not ready:
                problems.append(f"{unit}: " + ", ".join(f"{key}={state.get(key, 'unknown')}" for key in SERVICE_PROPERTIES))
        except HealthError as error:
            problems.append(f"{unit}: {error}")
    for name, destination, path, interface, method in (
        ("homed", "org.freedesktop.home1", "/org/freedesktop/home1", "org.freedesktop.home1.Manager", "ListHomes"),
        ("AccountsService", "org.freedesktop.Accounts", "/org/freedesktop/Accounts", "org.freedesktop.Accounts", "ListCachedUsers"),
    ):
        try:
            command(["busctl", "--system", "call", destination, path, interface, method], deadline)
        except HealthError as error:
            problems.append(f"{name} is not responding: {error}")
    return problems


def check_desktop(timeout=120):
    deadline = time.monotonic() + timeout
    storage_checks(deadline)
    last = None
    while True:
        problems = desktop_problems(deadline)
        if not problems:
            print("MertensiaOS local desktop checks passed", flush=True)
            return
        if problems != last:
            print("Waiting for desktop readiness: " + "; ".join(problems), flush=True)
            last = problems
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HealthError("desktop readiness timed out: " + "; ".join(problems))
        time.sleep(min(1, remaining))


def journal_messages(boot, deadline):
    output = command(["journalctl", "--boot=" + str(boot), "--unit=" + HEALTH_UNIT,
                      "--output=json", "--no-pager", "-n", "200"], deadline)
    return [json.loads(line).get("MESSAGE", "") for line in output.splitlines() if line.strip()]


def confirmed_fallback(current, previous, motd=""):
    # Require the successful operation, not merely Greenboot's intention to roll
    # back. A manual `bootc rollback` produces neither of these messages.
    return (any("Rollback to previous deployment completed successfully" in message for message in current)
            or (any("FALLBACK BOOT DETECTED! Default bootc deployment has been rolled back" in message for message in [*current, motd])
                and any("Rollback successful" in message for message in previous)))


def atomic_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def collect_status(path=EVENT):
    deadline = time.monotonic() + 25
    state = service_state(HEALTH_UNIT, deadline)
    if state.get("ActiveState") != "active" or state.get("Result") != "success":
        return
    try:
        motd = Path("/etc/motd.d/boot-status").read_text()
    except OSError:
        motd = ""
    current = journal_messages(0, deadline)
    try:
        previous = journal_messages(-1, deadline)
    except HealthError:
        previous = []  # First boot has no previous journal; kernel fallback does not need one.
    # The service can finish before journald has consumed its last log lines.
    # Its synchronously written MOTD identifies a fallback that needs a retry.
    while not confirmed_fallback(current, previous, motd):
        if "FALLBACK BOOT DETECTED!" not in motd or deadline - time.monotonic() < 2:
            return
        time.sleep(1)
        current = journal_messages(0, deadline)
    status = json.loads(command(["bootc", "status", "--json"], deadline))
    digest = status["status"]["booted"]["image"]["imageDigest"]
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    atomic_json(path, {"version": 1, "event_id": "rollback-" + boot_id, "boot_id": boot_id,
                       "timestamp": datetime.now(timezone.utc).isoformat(), "restored_image_digest": digest})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "collect"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise HealthError("boot-health system operations require root")
        if not installed_system():
            print("Skipping boot health outside the installed system")
            return 0
        if args.operation == "check":
            check_desktop()
        else:
            collect_status()
        return 0
    except (HealthError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        print(f"MertensiaOS boot health: {error}", flush=True)
        return 1
