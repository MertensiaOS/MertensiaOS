"""Fixed, administrator-authorized desktop operations; never accept commands or units."""

import argparse
import grp
import os
import pwd
import re
import sys

from mertensia.boot_health.backend import HealthError
from . import backend


TIMER = "bootc-fetch-apply-updates.timer"
SERVICE = "bootc-fetch-apply-updates.service"
OPERATIONS = ("download", "resume", "enable", "disable")


def authorize():
    if os.geteuid() != 0:
        raise backend.UpdateError("OS update operations require administrator authorization.")
    raw_uid = os.environ.get("PKEXEC_UID", "")
    if not re.fullmatch(r"[0-9]+", raw_uid) or int(raw_uid) < 1000:
        raise backend.UpdateError("A trusted desktop caller is required.")
    try:
        user = pwd.getpwuid(int(raw_uid))
        wheel = grp.getgrnam("wheel").gr_gid
        if user.pw_name == "mertensia-setup" or wheel not in os.getgrouplist(user.pw_name, user.pw_gid):
            raise backend.UpdateError("Only an administrator can change OS updates.")
    except KeyError as error:
        raise backend.UpdateError("The administrator account could not be verified.") from error
    if not backend.installed_system():
        raise backend.UpdateError("OS updates are available only on an installed MertensiaOS system.")


def perform(operation):
    if operation not in OPERATIONS:
        raise backend.UpdateError("Unsupported OS update operation.")
    authorize()
    if operation == "download":
        # Use the same guarded service as the timer; it outlives the desktop app.
        backend.command(["systemctl", "start", "--no-block", SERVICE])
    elif operation == "resume":
        with backend.locked(blocking=False):
            backend.resume()
    else:
        backend.command(["systemctl", operation, "--now", TIMER])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    args = parser.parse_args(argv)
    try:
        perform(args.operation)
        return 0
    except (backend.UpdateError, HealthError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
