"""Test the actual token conversation and oo7 re-encryption, without a real home.

Only pam_systemd_home is replaced with pam_permit in a private test service.
Native unit tests cover requisite failure ordering; VM integration covers homed.
"""
import ctypes
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import dbus

sys.path.insert(0, "/usr/lib/mertensia/python")
from mertensia.accounts import passwords


OLD = "old λ password"
NEW = "new λ password"


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def run():
    with tempfile.TemporaryDirectory(prefix="mertensia-keyring-") as directory:
        root = Path(directory)
        for part in ("home", "data", "config", "pam"):
            (root / part).mkdir(mode=0o700)
        os.environ.update(HOME=str(root / "home"), XDG_DATA_HOME=str(root / "data"),
                          XDG_CONFIG_HOME=str(root / "config"), XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
        bus = dbus.SessionBus()
        daemon = None
        report = {}

        def start(password):
            nonlocal daemon
            daemon = subprocess.Popen(["/usr/libexec/oo7-daemon", "--login"], stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # --login consumes exact bytes; a trailing newline becomes part of
            # the keyring password and would invalidate the synchronization test.
            daemon.stdin.write(password.encode())
            daemon.stdin.close()
            socket = Path(os.environ["XDG_RUNTIME_DIR"]) / "oo7-pam.sock"
            for _ in range(100):
                if bus.name_has_owner("org.freedesktop.secrets") and socket.is_socket():
                    return
                check(daemon.poll() is None, "oo7 exited before acquiring Secret Service")
                time.sleep(.05)
            raise RuntimeError("oo7 startup timed out")

        def stop():
            daemon.terminate()
            daemon.wait(timeout=10)
            for _ in range(100):
                if not bus.name_has_owner("org.freedesktop.secrets"):
                    return
                time.sleep(.05)
            raise RuntimeError("oo7 did not release Secret Service")

        def locked():
            service = dbus.Interface(bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets"),
                                     "org.freedesktop.Secret.Service")
            path = service.ReadAlias("default", timeout=10)
            return bool(dbus.Interface(bus.get_object("org.freedesktop.secrets", path),
                                       "org.freedesktop.DBus.Properties").Get(
                                           "org.freedesktop.Secret.Collection", "Locked", timeout=10))

        def tool(*arguments, input=None):
            result = subprocess.run(["secret-tool", *arguments], input=input, text=True, capture_output=True, timeout=20)
            check(result.returncode == 0, "secret-tool " + arguments[0] + " failed")
            return result.stdout.rstrip("\n")

        token_module = os.environ.get("MERTENSIA_TEST_TOKEN_MODULE", "/usr/lib64/security/pam_mertensia_password.so")
        (root / "pam/mertensia-password").write_text(
            f"password requisite {token_module}\n"
            "password [success=ok default=die] pam_permit.so\n"
            "password optional pam_oo7.so use_authtok\n")
        pam = passwords._libpam()
        pam.pam_start_confdir.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                         ctypes.POINTER(passwords.Conversation), ctypes.c_char_p,
                                         ctypes.POINTER(ctypes.c_void_p)]

        def private_start(service, username, conversation, handle):
            return pam.pam_start_confdir(service, username, conversation, os.fsencode(root / "pam"), handle)

        adapter = SimpleNamespace(pam_start=private_start, pam_chauthtok=pam.pam_chauthtok, pam_end=pam.pam_end)
        try:
            start(OLD)
            tool("store", "--label=Mertensia disposable keyring test", "mertensia-test", "password", input="test-secret")
            check(tool("lookup", "mertensia-test", "password") == "test-secret", "baseline secret mismatch")
            report["store_and_retrieve"] = True
            stop()
            start(NEW)
            check(locked(), "new password unexpectedly unlocked unchanged keyring")
            report["unchanged_keyring_rejects_new_password"] = True
            stop()
            start(OLD)
            files = list((root / "data").rglob("*.keyring"))
            check(len(files) == 1, "expected one persistent test keyring")
            before = files[0].read_bytes()
            with mock.patch.object(passwords, "_libpam", return_value=adapter):
                passwords.change_password(pwd.getpwuid(os.getuid()).pw_name, OLD, NEW)
            # oo7's PAM hook sends asynchronously and cannot acknowledge the
            # completed write. Wait for the actual encrypted file to change.
            for _ in range(100):
                if files[0].read_bytes() != before:
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("oo7 did not persist the password change")
            stop()
            start(NEW)
            check(not locked(), "new password did not unlock changed keyring")
            check(tool("lookup", "mertensia-test", "password") == "test-secret", "saved secret did not survive")
            report["new_password_unlocks_after_restart"] = True
            report["saved_secret_preserved"] = True
            stop()
            start(OLD)
            check(locked(), "old password still unlocks changed keyring")
            report["old_password_rejected_after_restart"] = True
            stop()
            start(NEW)
            tool("clear", "mertensia-test", "password")
            report["test_secret_removed"] = True
        finally:
            if daemon is not None and daemon.poll() is None:
                stop()
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
