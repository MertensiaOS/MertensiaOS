"""Systemd-homed, regional settings, and setup-account retirement operations."""
from __future__ import annotations

import grp
import json
import pwd
import subprocess
from typing import Any

import dbus

from . import state
from .state import LifecyclePaths
from .validation import RequestError, SETUP_USER, validate_password


def hash_password(password: str) -> str:
    """Create the persistent UNIX hash required by systemd-homed.

    The password is provided over a pipe rather than in argv. Validation rejects
    line separators because ``openssl passwd -stdin`` consumes one line.
    """

    validate_password(password)
    result = subprocess.run(
        ["/usr/bin/openssl", "passwd", "-6", "-stdin"],
        input=password + "\n",
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    password_hash = result.stdout.strip()
    if not password_hash.startswith("$6$") or "\n" in password_hash:
        raise RequestError("failed to generate a password hash")
    return password_hash


class SystemOperations:
    def __init__(self) -> None:
        self._manager: Any | None = None

    @property
    def manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        bus = dbus.SystemBus()
        manager_object = bus.get_object(
            "org.freedesktop.home1", "/org/freedesktop/home1"
        )
        self._manager = dbus.Interface(manager_object, "org.freedesktop.home1.Manager")
        return self._manager

    def home_record(self, username: str) -> dict[str, Any] | None:
        try:
            record_json, _incomplete, _path = self.manager.GetUserRecordByName(username)
        except dbus.DBusException as error:
            if error.get_dbus_name() in {
                "org.freedesktop.home1.NoSuchHome",
                "io.systemd.UserDatabase.NoRecordFound",
            }:
                return None
            raise
        record = json.loads(str(record_json))
        if not isinstance(record, dict):
            raise RequestError("systemd-homed returned an invalid user record")
        return record

    def create_home(self, data: dict[str, Any]) -> None:
        record = {
            "userName": data["username"],
            "realName": data["real_name"],
            "storage": "luks",
            "fileSystemType": "ext4",
            "memberOf": ["wheel"] if data["admin"] else [],
            "enforcePasswordPolicy": True,
            "privileged": {"hashedPassword": [hash_password(data["password"])]},
            # CreateHome takes one complete JSON user record. Secret material
            # belongs in its secret section and is not persisted in the public
            # record returned by homed.
            "secret": {"password": [data["password"]]},
        }
        self.manager.CreateHome(json.dumps(record, separators=(",", ":")))

    def ensure_name_available(self, username: str) -> None:
        # Homed rejects both NSS user and group conflicts, including accounts
        # outside its own database. Check before committing setup identity.
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup(username)
            except KeyError:
                continue
            raise RequestError("the requested username is already in use")

    def authenticate_home(self, username: str, password: str) -> None:
        """Verify recovery credentials against an existing homed account."""

        secret = json.dumps({"password": [password]}, separators=(",", ":"))
        try:
            self.manager.AuthenticateHome(username, secret)
        except dbus.DBusException as error:
            raise RequestError(
                "the password does not unlock the recovered initial administrator"
            ) from error

    def configure_machine(self, data: dict[str, Any]) -> None:
        subprocess.run(
            ["/usr/bin/hostnamectl", "set-hostname", data["hostname"]], check=True
        )
        subprocess.run(
            ["/usr/bin/localectl", "set-locale", f"LANG={data['locale']}"], check=True
        )
        subprocess.run(
            ["/usr/bin/localectl", "set-keymap", data["keymap"]], check=True
        )
        subprocess.run(
            ["/usr/bin/timedatectl", "set-timezone", data["timezone"]], check=True
        )

    def retire_setup_account(self, paths: LifecyclePaths) -> None:
        # Idempotent: the path unit invokes this again after updates and on
        # boots where an earlier retirement attempt was interrupted.
        state.atomic_write(
            paths.gdm_config,
            "[daemon]\nAutomaticLoginEnable=False\n",
            0o644,
        )
        subprocess.run(
            [
                "/usr/sbin/usermod",
                "--lock",
                "--shell",
                "/usr/sbin/nologin",
                SETUP_USER,
            ],
            check=True,
        )
        state.atomic_write(paths.disabled_marker, "disabled\n", 0o644)

    def schedule_session_termination(self) -> None:
        subprocess.run(
            [
                "/usr/bin/systemctl",
                "start",
                "--no-block",
                "mertensia-firstboot-finish.timer",
            ],
            check=True,
        )
