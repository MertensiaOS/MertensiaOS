"""Durable setup state and the lock protecting account lifecycle transitions."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .validation import RequestError


@dataclass(frozen=True)
class LifecyclePaths:
    marker: Path = Path("/var/lib/mertensia/firstboot-complete")
    disabled_marker: Path = Path("/var/lib/mertensia/setup-account-disabled")
    state: Path = Path("/var/lib/mertensia/firstboot-state.json")
    lock: Path = Path("/run/lock/mertensia/accounts.lock")
    gdm_config: Path = Path("/etc/gdm/custom.conf")


DEFAULT_PATHS = LifecyclePaths()


def atomic_write(path: Path, contents: str, mode: int = 0o644) -> None:
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


def write_state(paths: LifecyclePaths, state: dict[str, Any]) -> None:
    atomic_write(paths.state, json.dumps(state, sort_keys=True) + "\n", 0o600)


def load_state(paths: LifecyclePaths) -> dict[str, Any] | None:
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
def lifecycle_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
