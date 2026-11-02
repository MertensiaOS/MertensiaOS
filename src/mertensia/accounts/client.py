"""Unprivileged desktop client for homed, AccountsService and the admin helper."""
from __future__ import annotations

from dataclasses import dataclass
import grp
import json
import os
import pwd
from pathlib import Path
import stat
import subprocess

import dbus

from .validation import RequestError, validate_password
from . import passwords


@dataclass(frozen=True)
class Identity:
    username: str
    uid: int
    administrator: bool


@dataclass(frozen=True)
class Account:
    username: str
    uid: int
    name: str
    administrator: bool
    locked: bool
    signed_in: bool
    language: str
    avatar: str
    state: str


def current_identity() -> Identity:
    user = pwd.getpwuid(os.getuid())
    try:
        administrator = grp.getgrnam("wheel").gr_gid in os.getgrouplist(user.pw_name, user.pw_gid)
    except (KeyError, OSError):
        administrator = False
    return Identity(user.pw_name, user.pw_uid, administrator)


def error_message(error: Exception) -> str:
    if isinstance(error, dbus.DBusException):
        name = error.get_dbus_name()
        if name in {"org.freedesktop.home1.BadPassword", "org.freedesktop.home1.BadPasswordAndNoToken"}:
            return "The current password is incorrect. Try again."
        if name in {"org.freedesktop.home1.LowPasswordQuality", "org.freedesktop.home1.BadPasswordQuality"}:
            return "Choose a stronger password and try again."
        if name == "org.freedesktop.home1.AuthenticationLimitHit":
            return "Too many attempts. Wait a little before trying again."
        if name in {"org.freedesktop.home1.HomeBusy", "org.freedesktop.home1.HomeInUse"}:
            return "This user's home is in use. Ask them to sign out, then try again."
        if name in {"org.freedesktop.DBus.Error.AccessDenied", "org.freedesktop.Accounts.Error.PermissionDenied"}:
            return "Authorization was denied or cancelled. Try again."
        return "The account service could not complete this change. Refresh and try again."
    if isinstance(error, RequestError):
        return str(error)
    return "Could not contact the account service. Refresh and try again."


class AccountsClient:
    def __init__(self, bus=None):
        # Each worker creates its own private connection; no dbus-python
        # connection or interface is shared between GTK and worker threads.
        self.bus = bus if bus is not None else dbus.SystemBus(private=True)
        self.home = dbus.Interface(self.bus.get_object("org.freedesktop.home1", "/org/freedesktop/home1"),
                                   "org.freedesktop.home1.Manager")

    def accounts(self) -> list[Account]:
        login = dbus.Interface(self.bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                               "org.freedesktop.login1.Manager")
        signed_in = {int(session[1]) for session in login.ListSessions(timeout=10)}
        service = dbus.Interface(self.bus.get_object("org.freedesktop.Accounts", "/org/freedesktop/Accounts"),
                                 "org.freedesktop.Accounts")
        result = []
        for username, uid, state, _gid, name, _directory, _shell, _path in self.home.ListHomes(timeout=30):
            if int(uid) < 1000 or str(username) == "mertensia-setup":
                continue
            path = service.FindUserByName(str(username), timeout=30)
            properties = dbus.Interface(self.bus.get_object("org.freedesktop.Accounts", path),
                                        "org.freedesktop.DBus.Properties")
            # AccountsService resolves homed's per-machine settings and binding
            # section, including the avatar blob directory, into public fields.
            profile = properties.GetAll("org.freedesktop.Accounts.User", timeout=30)
            result.append(Account(str(username), int(uid), str(profile.get("RealName") or name or username),
                                  int(profile.get("AccountType", 0)) == 1, bool(profile.get("Locked", False)),
                                  int(uid) in signed_in, str(profile.get("Language") or ""),
                                  str(profile.get("IconFile") or ""), str(state)))
        identity = current_identity()
        return sorted(result, key=lambda account: (account.uid != identity.uid, account.name.casefold()))

    def change_password(self, username: str, current: str, password: str) -> str | None:
        if username != current_identity().username:
            raise RequestError("Sign in to this account to change its password.")
        if not current or "\x00" in current:
            raise RequestError("Enter your current password.")
        validate_password(password)
        if password == current:
            raise RequestError("Choose a password different from your current password.")
        self._authenticate_password(username, current)
        try:
            passwords.change_password(username, current, password)
        except passwords.PamError as error:
            # PAM is not transactional. Never encourage retrying an already
            # committed home change because a later module returned an error.
            try:
                self._authenticate_password(username, password)
            except dbus.DBusException as verification:
                if verification.get_dbus_name() not in {
                    "org.freedesktop.home1.BadPassword", "org.freedesktop.home1.BadPasswordAndNoToken",
                }:
                    raise RequestError("Could not confirm whether your password changed. Check which password "
                                       "unlocks your home before trying another change.") from None
                try:
                    self._authenticate_password(username, current)
                except dbus.DBusException:
                    raise RequestError("Could not confirm whether your password changed. Check which password "
                                       "unlocks your home before trying another change.") from None
                raise RequestError("The password was not changed. Check the password service and try again.") from error
            return ("Your login password changed, but the remaining password updates could not be confirmed. "
                    "Use your new password to sign in. A saved-password keyring may still need your previous password.")
        # The optional oo7 hook cannot acknowledge re-encryption to PAM. Report
        # a known unavailable listener; retain the general caveat in the form.
        try:
            listener = Path(f"/run/user/{os.getuid()}/oo7-pam.sock").lstat()
            ready = stat.S_ISSOCK(listener.st_mode) and listener.st_uid == os.getuid()
        except OSError:
            ready = False
        if not ready:
            return ("Your login password changed. The login keyring service was unavailable, so saved passwords "
                    "may still need your previous password. Keep it until you have checked your saved passwords.")
        return None

    def _authenticate_password(self, username: str, password: str) -> None:
        self.home.AuthenticateHome(username, json.dumps({"password": [password]}, separators=(",", ":")), timeout=120)

    def set_profile(self, username: str, field: str, value: str) -> None:
        # AccountsService already supports homed metadata and avatar blobs.
        # Its per-user authorization checks remain authoritative.
        service = dbus.Interface(self.bus.get_object("org.freedesktop.Accounts", "/org/freedesktop/Accounts"),
                                 "org.freedesktop.Accounts")
        path = service.FindUserByName(username, timeout=30)
        user = dbus.Interface(self.bus.get_object("org.freedesktop.Accounts", path), "org.freedesktop.Accounts.User")
        if field == "name":
            value = value.strip()
            if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
                raise RequestError("Enter a name of up to 128 characters, without control characters.")
            user.SetRealName(value, timeout=120)
        elif field == "language":
            if value and value not in available_languages():
                raise RequestError("Choose an installed language.")
            user.SetLanguage(value, timeout=120)
        elif field == "avatar":
            user.SetIconFile(value, timeout=120)
        else:
            raise RequestError("unsupported profile setting")


def available_languages() -> list[str]:
    result = subprocess.run(["/usr/bin/locale", "-a"], text=True, capture_output=True, check=True, timeout=10)
    return sorted({locale for locale in result.stdout.splitlines() if ".utf" in locale.lower() and "_" in locale})


def administrator_request(request: dict) -> dict:
    result = subprocess.run(["/usr/bin/pkexec", "/usr/libexec/mertensia-accounts-helper"],
                            input=json.dumps(request) + "\n", text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if not result.stdout.strip():
        if result.returncode == 126:
            raise RequestError("Authentication was cancelled. You can try again.")
        raise RequestError("Authorization failed. Sign in as an administrator and try again.")
    response = json.loads(result.stdout.splitlines()[-1])
    if not response.get("ok") or result.returncode != 0:
        raise RequestError(response.get("message", "The account could not be changed."))
    return response
