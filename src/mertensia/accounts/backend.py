#!/usr/bin/python3
"""Privileged account-management backend for MertensiaOS.

Request JSON is untrusted. In particular, its ``firstboot`` and ``admin``
fields do not decide which operation is allowed. That decision is derived
from PKEXEC_UID and persisted setup state while holding an exclusive lock.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping

import dbus

from . import state as storage
from .operations import SystemOperations
from .state import DEFAULT_PATHS, LifecyclePaths
from .validation import RequestError, SETUP_USER, validate_request


# Kept as a source-compatible name for callers/tests from the original helper.
SetupError = RequestError


@dataclass(frozen=True)
class Caller:
    uid: int
    username: str
    administrator: bool


@dataclass(frozen=True)
class RequestResult:
    username: str
    initial_setup: bool
    retirement_pending: bool = False


def _reply(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


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


def _record_is_administrator(record: dict[str, Any]) -> bool:
    memberships = record.get("memberOf", [])
    return isinstance(memberships, list) and "wheel" in memberships


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
    state = storage.load_state(paths)
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
        storage.write_state(paths, state)
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
    storage.write_state(paths, state)
    operations.configure_machine(effective_data)
    state["phase"] = "machine-configured"
    storage.write_state(paths, state)

    # This durable marker is the authorization boundary. Both the helper and
    # the polkit rule deny the setup identity as soon as it exists.
    try:
        storage.atomic_write(paths.marker, "complete\n", 0o644)
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
        storage.write_state(paths, state)
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
    with storage.lifecycle_lock(paths.lock):
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
        with storage.lifecycle_lock(paths.lock):
            operations = SystemOperations()
            operations.retire_setup_account(paths)
            state = storage.load_state(paths)
            if state is not None:
                state["phase"] = "complete"
                storage.write_state(paths, state)
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
