"""Validate untrusted account requests without desktop or D-Bus dependencies."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


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


def validate_password(password: Any) -> None:
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
    validate_password(password)

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
