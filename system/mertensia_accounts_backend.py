#!/usr/bin/python3
"""Privileged account-management backend for MertensiaOS.

Request JSON is untrusted. In particular, its ``firstboot`` and ``admin``
fields do not decide which operation is allowed. That decision is derived
from PKEXEC_UID and persisted setup state while holding an exclusive lock.
"""

from __future__ import annotations

import fcntl
import grp
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import dbus


SETUP_USER = "mertensia-setup"
MAX_PASSWORD_BYTES = 256  # openssl passwd's input limit
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-zA-Z0-9-]{1,63})(?<!-)"
    r"(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*$"
)
LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}_[A-Za-z]{2}(?:\.[A-Za-z0-9@_-]+)?$")
KEYMAP_RE = re.compile(r"^[A-Za-z0-9_+.-]{1,64}$")


class RequestError(Exception):
    """A request cannot be safely fulfilled."""


# Kept as a source-compatible name for callers/tests from the original helper.
SetupError = RequestError


@dataclass(frozen=True)
class Caller:
    uid: int
    username: str
    administrator: bool


@dataclass(frozen=True)
class LifecyclePaths:
    marker: Path = Path("/var/lib/mertensia/firstboot-complete")
    disabled_marker: Path = Path("/var/lib/mertensia/setup-account-disabled")
    state: Path = Path("/var/lib/mertensia/firstboot-state.json")
    lock: Path = Path("/run/lock/mertensia/accounts.lock")
    gdm_config: Path = Path("/etc/gdm/custom.conf")


@dataclass(frozen=True)
class RequestResult:
    username: str
    initial_setup: bool
    retirement_pending: bool = False


DEFAULT_PATHS = LifecyclePaths()


def _reply(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _atomic_write(path: Path, contents: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_state(paths: LifecyclePaths, state: dict[str, Any]) -> None:
    _atomic_write(paths.state, json.dumps(state, sort_keys=True) + "\n", 0o600)


def _load_state(paths: LifecyclePaths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    try:
        state = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RequestError("initial setup recovery state is unreadable") from error
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RequestError("initial setup recovery state is invalid")
    if state.get("phase") not in {
        "creating-home",
        "home-created",
        "machine-configured",
        "retirement-pending",
        "complete",
    }:
        raise RequestError("initial setup recovery state has an invalid phase")
    if not isinstance(state.get("username"), str) or not isinstance(
        state.get("configuration"), dict
    ):
        raise RequestError("initial setup recovery state is incomplete")
    return state


@contextmanager
def _lifecycle_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _is_administrator(username: str, primary_gid: int) -> bool:
    try:
        wheel_gid = grp.getgrnam("wheel").gr_gid
        return wheel_gid in os.getgrouplist(username, primary_gid)
    except (KeyError, OSError):
        return False


def identify_pkexec_caller(environment: Mapping[str, str] | None = None) -> Caller:
    """Return the original pkexec caller, failing closed on missing metadata."""

    environment = os.environ if environment is None else environment
    raw_uid = environment.get("PKEXEC_UID")
    if raw_uid is None or re.fullmatch(r"[0-9]+", raw_uid) is None:
        raise RequestError("trusted pkexec caller identity is unavailable")
    uid = int(raw_uid, 10)
    if uid <= 0:
        raise RequestError("a non-root pkexec caller is required")
    try:
        record = pwd.getpwuid(uid)
    except KeyError as error:
        raise RequestError("the pkexec caller does not resolve to a local user") from error
    if record.pw_uid != uid or not record.pw_name:
        raise RequestError("the pkexec caller identity is inconsistent")
    return Caller(uid, record.pw_name, _is_administrator(record.pw_name, record.pw_gid))


def _require_string(data: dict[str, Any], key: str, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise RequestError(f"{key} is invalid")
    return value


def _timezone_exists(timezone: str) -> bool:
    zone = Path("/usr/share/zoneinfo") / timezone
    try:
        resolved = zone.resolve(strict=True)
        resolved.relative_to(Path("/usr/share/zoneinfo"))
    except (OSError, ValueError):
        return False
    return resolved.is_file()


def _validate_password(password: Any) -> None:
    if not isinstance(password, str) or len(password) < 8:
        raise RequestError("password must contain at least 8 characters")
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RequestError("password is invalid") from error
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise RequestError("password must not exceed 256 UTF-8 bytes")
    if any(character in password for character in ("\x00", "\n", "\r")):
        raise RequestError("password is invalid")


def validate_request(data: Any, initial_setup: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RequestError("request must be a JSON object")

    username = _require_string(data, "username", 31)
    if USERNAME_RE.fullmatch(username) is None or username == SETUP_USER:
        raise RequestError("username is invalid")

    real_name = _require_string(data, "real_name", 128)
    password = data.get("password")
    _validate_password(password)

    validated: dict[str, Any] = {
        "username": username,
        "real_name": real_name,
        "password": password,
        "admin": True if initial_setup else data.get("admin") is True,
    }

    if initial_setup:
        hostname = _require_string(data, "hostname", 253)
        locale = _require_string(data, "locale", 64)
        timezone = _require_string(data, "timezone", 128)
        keymap = _require_string(data, "keymap", 64)
        if HOSTNAME_RE.fullmatch(hostname) is None:
            raise RequestError("hostname is invalid")
        if LOCALE_RE.fullmatch(locale) is None:
            raise RequestError("locale is invalid")
        if timezone.startswith("/") or ".." in timezone.split("/") or not _timezone_exists(timezone):
            raise RequestError("timezone is invalid")
        if KEYMAP_RE.fullmatch(keymap) is None:
            raise RequestError("keymap is invalid")
        validated.update(
            hostname=hostname,
            locale=locale,
            timezone=timezone,
            keymap=keymap,
        )

    return validated


def _record_is_administrator(record: dict[str, Any]) -> bool:
    memberships = record.get("memberOf", [])
    return isinstance(memberships, list) and "wheel" in memberships


def _hash_password(password: str) -> str:
    """Create the persistent UNIX hash required by systemd-homed.

    The password is provided over a pipe rather than in argv. Validation rejects
    line separators because ``openssl passwd -stdin`` consumes one line.
    """

    _validate_password(password)
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
            "privileged": {"hashedPassword": [_hash_password(data["password"])]},
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
        _atomic_write(
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
        _atomic_write(paths.disabled_marker, "disabled\n", 0o644)

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


def _setup_configuration(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": data["username"],
        "real_name": data["real_name"],
        "admin": True,
        "hostname": data["hostname"],
        "locale": data["locale"],
        "timezone": data["timezone"],
        "keymap": data["keymap"],
    }


def _process_initial_setup(
    data: dict[str, Any], paths: LifecyclePaths, operations: SystemOperations
) -> RequestResult:
    state = _load_state(paths)
    if state is not None and state["username"] != data["username"]:
        # A failed creation may have left only our intent record. Never switch
        # identity once homed knows the original account (including an ongoing
        # creation), or after setup has progressed beyond creating it.
        if state["phase"] != "creating-home" or operations.home_record(state["username"]) is not None:
            raise RequestError(
                "initial setup is already in progress for a different administrator"
            )
        state = None
    if state is None:
        if operations.home_record(data["username"]) is not None:
            raise RequestError("the requested initial administrator already exists")
        operations.ensure_name_available(data["username"])
        configuration = _setup_configuration(data)
        state = {
            "version": 1,
            "phase": "creating-home",
            "username": data["username"],
            "configuration": configuration,
        }
        _write_state(paths, state)
    else:
        configuration = state["configuration"]

    effective_data = dict(data)
    effective_data.update(configuration)
    effective_data["admin"] = True

    record = operations.home_record(data["username"])
    recovering_existing_home = record is not None
    if record is None:
        operations.create_home(effective_data)
        record = operations.home_record(data["username"])
        if record is None:
            raise RequestError("the initial administrator was not created")
    if not _record_is_administrator(record):
        raise RequestError("the recovered initial account is not an administrator")
    if recovering_existing_home:
        # A failure after CreateHome must not let a retry silently finish with
        # a newly supplied password that does not unlock the existing account.
        operations.authenticate_home(data["username"], data["password"])

    # Retain the account identity, but accept corrections to machine settings
    # only after an existing administrator's password has been verified.
    for key in ("hostname", "locale", "timezone", "keymap"):
        configuration[key] = data[key]
        effective_data[key] = data[key]
    state["configuration"] = configuration
    state["phase"] = "home-created"
    _write_state(paths, state)
    operations.configure_machine(effective_data)
    state["phase"] = "machine-configured"
    _write_state(paths, state)

    # This durable marker is the authorization boundary. Both the helper and
    # the polkit rule deny the setup identity as soon as it exists.
    try:
        _atomic_write(paths.marker, "complete\n", 0o644)
    except OSError as error:
        # A directory fsync can fail after the marker was already replaced.
        # Once it exists the setup caller is denied, so report the committed
        # account and continue retirement rather than offering an invalid retry.
        if not paths.marker.exists():
            raise
        print(f"unable to flush setup completion marker: {error}", file=sys.stderr)

    retirement_pending = False
    try:
        operations.retire_setup_account(paths)
    except Exception:
        retirement_pending = True
        state["phase"] = "retirement-pending"
    else:
        state["phase"] = "complete"
    try:
        _write_state(paths, state)
    except Exception as error:
        # The completion marker has already revoked setup authorization. The
        # enabled retirement path repairs this state; bookkeeping must not
        # turn a committed setup into a failed request or prevent sign-in.
        print(f"unable to record completed setup state: {error}", file=sys.stderr)
    return RequestResult(data["username"], True, retirement_pending)


def process_request(
    request: Any,
    caller: Caller,
    paths: LifecyclePaths = DEFAULT_PATHS,
    operations: SystemOperations | None = None,
) -> RequestResult:
    operations = SystemOperations() if operations is None else operations
    with _lifecycle_lock(paths.lock):
        complete = paths.marker.exists() or paths.disabled_marker.exists()

        if caller.username == SETUP_USER:
            if complete:
                raise RequestError("initial setup is already complete")
            # A false/missing client flag must never turn the setup identity
            # into the normal administrator account-creation path.
            if not isinstance(request, dict) or request.get("firstboot") is not True:
                raise RequestError("the setup account may only perform initial setup")
            data = validate_request(request, True)
            return _process_initial_setup(data, paths, operations)

        if not complete:
            raise RequestError("normal account management is unavailable before setup")
        if not caller.administrator:
            raise RequestError("the caller is not an administrator")
        if isinstance(request, dict) and request.get("firstboot") is True:
            raise RequestError("initial setup may only be performed by the setup account")

        data = validate_request(request, False)
        if operations.home_record(data["username"]) is not None:
            raise RequestError("the requested account already exists")
        operations.ensure_name_available(data["username"])
        operations.create_home(data)
        if operations.home_record(data["username"]) is None:
            raise RequestError("the account was not created")
        return RequestResult(data["username"], False)


def _retire_from_systemd(paths: LifecyclePaths = DEFAULT_PATHS) -> int:
    if os.geteuid() != 0:
        print("setup account retirement requires root", file=sys.stderr)
        return 1
    if not paths.marker.exists():
        print("initial setup is not complete", file=sys.stderr)
        return 1
    try:
        with _lifecycle_lock(paths.lock):
            operations = SystemOperations()
            operations.retire_setup_account(paths)
            state = _load_state(paths)
            if state is not None:
                state["phase"] = "complete"
                _write_state(paths, state)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if os.geteuid() != 0:
        _reply(
            {
                "event": "error",
                "ok": False,
                "message": "account management requires root",
            }
        )
        return 1

    if sys.argv[1:] == ["--retire-setup-account"]:
        return _retire_from_systemd()
    if sys.argv[1:]:
        _reply(
            {
                "event": "error",
                "ok": False,
                "message": "unsupported helper arguments",
            }
        )
        return 1

    result: RequestResult | None = None
    try:
        caller = identify_pkexec_caller()
        raw_request = sys.stdin.readline(65537)
        if len(raw_request) > 65536:
            raise RequestError("request is too large")
        request = json.loads(raw_request)
        result = process_request(request, caller)
        response: dict[str, Any] = {
            "event": "complete",
            "ok": True,
            "username": result.username,
            "firstboot": result.initial_setup,
        }
        warnings = []
        if result.retirement_pending:
            warnings.append("Setup will finish automatically.")
        if result.initial_setup:
            try:
                SystemOperations().schedule_session_termination()
            except Exception as error:
                # Return the committed result with an actionable fallback.
                # The retirement path still repairs account/GDM state.
                print(f"unable to schedule setup-session termination: {error}", file=sys.stderr)
                warnings.append(
                    "The sign-in screen could not open automatically. "
                    "Restart this computer to sign in."
                )
        if warnings:
            response["warning"] = " ".join(warnings)
        _reply(response)
    except (RequestError, json.JSONDecodeError, dbus.DBusException, subprocess.SubprocessError, OSError) as error:
        _reply({"event": "error", "ok": False, "message": str(error)})
        return 1
    except Exception as error:
        _reply(
            {
                "event": "error",
                "ok": False,
                "message": f"account creation failed: {error}",
            }
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
