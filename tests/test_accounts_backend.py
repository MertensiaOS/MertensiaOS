import importlib.util
import ctypes
import ctypes.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "system/mertensia_accounts_backend.py"
SPEC = importlib.util.spec_from_file_location("accounts_backend", MODULE_PATH)
accounts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = accounts
SPEC.loader.exec_module(accounts)


def setup_request(username="alice", firstboot=True):
    return {
        "username": username,
        "real_name": "Alice Example",
        "password": "correct horse battery staple",
        "admin": False,
        "firstboot": firstboot,
        "hostname": "mertensia-laptop",
        "locale": "en_US.UTF-8",
        "timezone": "Pacific/Auckland",
        "keymap": "us",
    }


def normal_request(username="bob", admin=True):
    return {
        "username": username,
        "real_name": "Bob Example",
        "password": "another correct horse battery staple",
        "admin": admin,
        "firstboot": False,
    }


class FakeOperations:
    def __init__(self, configure_failures=0, retirement_failures=0, create_delay=0):
        self.homes = {}
        self.create_count = 0
        self.authenticate_count = 0
        self.configure_count = 0
        self.retire_count = 0
        self.schedule_count = 0
        self.configure_failures = configure_failures
        self.retirement_failures = retirement_failures
        self.create_delay = create_delay
        self.passwords = {}
        self.guard = threading.Lock()

    def home_record(self, username):
        with self.guard:
            return self.homes.get(username)

    def ensure_name_available(self, username):
        pass

    def create_home(self, data):
        if self.create_delay:
            time.sleep(self.create_delay)
        with self.guard:
            self.create_count += 1
            self.homes[data["username"]] = {
                "userName": data["username"],
                "memberOf": ["wheel"] if data["admin"] else [],
            }
            self.passwords[data["username"]] = data["password"]

    def authenticate_home(self, username, password):
        self.authenticate_count += 1
        if self.passwords.get(username) != password:
            raise accounts.RequestError(
                "the password does not unlock the recovered initial administrator"
            )

    def configure_machine(self, data):
        self.configure_count += 1
        if self.configure_failures:
            self.configure_failures -= 1
            raise OSError("injected machine configuration failure")

    def retire_setup_account(self, paths):
        self.retire_count += 1
        if self.retirement_failures:
            self.retirement_failures -= 1
            raise OSError("injected setup-account retirement failure")
        accounts._atomic_write(paths.disabled_marker, "disabled\n")

    def schedule_session_termination(self):
        self.schedule_count += 1


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = accounts.LifecyclePaths(
            marker=root / "var/lib/mertensia/firstboot-complete",
            disabled_marker=root / "var/lib/mertensia/setup-account-disabled",
            state=root / "var/lib/mertensia/firstboot-state.json",
            lock=root / "run/lock/mertensia/accounts.lock",
            gdm_config=root / "etc/gdm/custom.conf",
        )
        self.setup_caller = accounts.Caller(1000, accounts.SETUP_USER, False)
        self.admin_caller = accounts.Caller(1001, "owner", True)

    def tearDown(self):
        self.temporary.cleanup()


class AccountBackendStaticTests(unittest.TestCase):
    def test_local_user_and_group_conflicts_are_rejected(self):
        operations = accounts.SystemOperations()
        for user_exists, group_exists in ((True, False), (False, True), (False, False)):
            with self.subTest(user_exists=user_exists, group_exists=group_exists), mock.patch.object(
                accounts.pwd, "getpwnam", side_effect=None if user_exists else KeyError
            ), mock.patch.object(
                accounts.grp, "getgrnam", side_effect=None if group_exists else KeyError
            ):
                if user_exists or group_exists:
                    with self.assertRaisesRegex(accounts.RequestError, "already in use"):
                        operations.ensure_name_available("candidate")
                else:
                    operations.ensure_name_available("candidate")

    def test_password_hash_matches_full_password_at_byte_limit(self):
        library = ctypes.util.find_library("crypt")
        if not library:
            self.skipTest("libcrypt unavailable")
        crypt = ctypes.CDLL(library).crypt
        crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        crypt.restype = ctypes.c_char_p
        for password in ("a" * 255 + "Z", "é" * 127 + "λ"):
            with self.subTest(password_bytes=len(password.encode("utf-8"))):
                hashed = accounts._hash_password(password).encode("ascii")
                self.assertEqual(crypt(password.encode("utf-8"), hashed), hashed)
                self.assertNotEqual(crypt(password[:-1].encode("utf-8"), hashed), hashed)

    def test_hashing_rejects_oversized_password_before_invoking_openssl(self):
        for password in ("a" * 257, "é" * 129):
            with self.subTest(password_bytes=len(password.encode("utf-8"))), mock.patch.object(
                accounts.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(accounts.RequestError, "256 UTF-8 bytes"):
                    accounts._hash_password(password)
                run.assert_not_called()

    def test_homed_configuration_requires_luks_and_ext4(self):
        config = (ROOT / "system/homed.conf").read_text(encoding="utf-8")
        self.assertIn("DefaultStorage=luks", config)
        self.assertIn("DefaultFileSystemType=ext4", config)

    def test_polkit_setup_access_checks_lifecycle(self):
        rule = (ROOT / "system/49-mertensia-accounts.rules").read_text(encoding="utf-8")
        checker = (ROOT / "system/mertensia-setup-authorized").read_text(encoding="utf-8")
        self.assertIn("mertensia-setup-authorized", rule)
        self.assertIn("firstboot-complete", checker)
        self.assertIn("setup-account-disabled", checker)
        self.assertIn("org.freedesktop.accounts.user-administration", rule)
        self.assertIn("polkit.Result.NO", rule)

    def test_retirement_recovery_is_enabled_in_the_image(self):
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("systemctl enable mertensia-firstboot-retire.path", containerfile)
        service = (ROOT / "system/mertensia-firstboot-retire.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("--retire-setup-account", service)

    def test_homed_create_uses_one_complete_user_record(self):
        operations = accounts.SystemOperations()
        operations._manager = SimpleNamespace(CreateHome=mock.Mock())
        with mock.patch.object(accounts, "_hash_password", return_value="$6$test-hash"):
            operations.create_home(
                {
                    "username": "alice",
                    "real_name": "Alice Example",
                    "password": "correct horse battery staple",
                    "admin": True,
                }
            )
        operations.manager.CreateHome.assert_called_once()
        arguments = operations.manager.CreateHome.call_args.args
        self.assertEqual(len(arguments), 1)
        record = json.loads(arguments[0])
        self.assertEqual(record["secret"]["password"], ["correct horse battery staple"])
        self.assertEqual(record["privileged"]["hashedPassword"], ["$6$test-hash"])
        self.assertEqual(record["memberOf"], ["wheel"])
        self.assertNotEqual(record.get("diskSize"), 0)

    def test_password_hash_is_not_passed_in_argv(self):
        completed = SimpleNamespace(stdout="$6$generated\n")
        with mock.patch.object(accounts.subprocess, "run", return_value=completed) as run:
            self.assertEqual(accounts._hash_password("secret value"), "$6$generated")
        self.assertNotIn("secret value", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], "secret value\n")

    def test_recovery_authenticates_existing_homed_account(self):
        operations = accounts.SystemOperations()
        operations._manager = SimpleNamespace(AuthenticateHome=mock.Mock())
        operations.authenticate_home("alice", "correct horse battery staple")
        operations.manager.AuthenticateHome.assert_called_once_with(
            "alice", '{"password":["correct horse battery staple"]}'
        )


class CallerIdentityTests(unittest.TestCase):
    def test_missing_or_malformed_pkexec_identity_fails_closed(self):
        for environment in ({}, {"PKEXEC_UID": ""}, {"PKEXEC_UID": "12x"}, {"PKEXEC_UID": "0"}):
            with self.subTest(environment=environment):
                with self.assertRaises(accounts.RequestError):
                    accounts.identify_pkexec_caller(environment)

    def test_pkexec_uid_resolves_original_user(self):
        passwd = SimpleNamespace(pw_uid=1001, pw_name="owner", pw_gid=1001)
        with mock.patch.object(accounts.pwd, "getpwuid", return_value=passwd), mock.patch.object(
            accounts, "_is_administrator", return_value=True
        ):
            caller = accounts.identify_pkexec_caller({"PKEXEC_UID": "1001"})
        self.assertEqual(caller, accounts.Caller(1001, "owner", True))


class AccountLifecycleTests(BackendTestCase):
    def run_setup_helper(self, operations):
        process_request = accounts.process_request
        output = io.StringIO()
        with mock.patch.object(accounts.os, "geteuid", return_value=0), \
             mock.patch.object(accounts, "identify_pkexec_caller", return_value=self.setup_caller), \
             mock.patch.object(accounts, "process_request", side_effect=lambda request, caller:
                               process_request(request, caller, self.paths, operations)), \
             mock.patch.object(accounts, "SystemOperations", return_value=operations), \
             mock.patch.object(accounts.sys, "argv", ["mertensia-accounts-helper"]), \
             mock.patch.object(accounts.sys, "stdin", io.StringIO(json.dumps(setup_request()) + "\n")), \
             mock.patch.object(accounts.sys, "stdout", output), \
             mock.patch.object(accounts.sys, "stderr", io.StringIO()):
            status = accounts.main()
        return status, json.loads(output.getvalue())

    def test_local_name_conflict_does_not_commit_setup_identity(self):
        operations = FakeOperations()
        with mock.patch.object(operations, "ensure_name_available", side_effect=accounts.RequestError("already in use")):
            with self.assertRaisesRegex(accounts.RequestError, "already in use"):
                accounts.process_request(setup_request("root"), self.setup_caller, self.paths, operations)
        self.assertFalse(self.paths.state.exists())
        self.assertEqual(operations.create_count, 0)
        result = accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)
        self.assertEqual(result.username, "alice")

    def test_failed_creation_without_home_allows_username_correction(self):
        operations = FakeOperations()
        with mock.patch.object(operations, "create_home", side_effect=OSError("creation failed")):
            with self.assertRaisesRegex(OSError, "creation failed"):
                accounts.process_request(setup_request("root"), self.setup_caller, self.paths, operations)
        self.assertEqual(accounts._load_state(self.paths)["phase"], "creating-home")
        self.assertEqual(operations.homes, {})
        result = accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)
        self.assertEqual(result.username, "alice")
        self.assertEqual(accounts._load_state(self.paths)["username"], "alice")
        self.assertEqual(set(operations.homes), {"alice"})

    def test_lost_creation_reply_does_not_allow_username_correction(self):
        operations = FakeOperations()
        create_home = operations.create_home

        def lost_reply(data):
            create_home(data)
            raise OSError("reply lost")

        with mock.patch.object(operations, "create_home", side_effect=lost_reply):
            with self.assertRaisesRegex(OSError, "reply lost"):
                accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)
        self.assertEqual(accounts._load_state(self.paths)["phase"], "creating-home")
        with self.assertRaisesRegex(accounts.RequestError, "different administrator"):
            accounts.process_request(setup_request("mallory"), self.setup_caller, self.paths, operations)
        result = accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)
        self.assertEqual(result.username, "alice")
        self.assertEqual(operations.create_count, 1)
        self.assertEqual(operations.authenticate_count, 1)

    def test_username_correction_fails_closed_when_home_lookup_fails(self):
        operations = FakeOperations()
        with mock.patch.object(operations, "create_home", side_effect=OSError("creation failed")):
            with self.assertRaises(OSError):
                accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)
        state = self.paths.state.read_bytes()
        with mock.patch.object(operations, "home_record", side_effect=OSError("homed unavailable")):
            with self.assertRaisesRegex(OSError, "homed unavailable"):
                accounts.process_request(setup_request("mallory"), self.setup_caller, self.paths, operations)
        self.assertEqual(self.paths.state.read_bytes(), state)

    def test_password_validation_enforces_utf8_byte_limit(self):
        for password, accepted in (("a" * 256, True), ("a" * 257, False),
                                   ("é" * 128, True), ("é" * 129, False),
                                   ("a" * 8 + "\ud800", False)):
            with self.subTest(password=repr(password)):
                request = dict(normal_request(), password=password)
                if accepted:
                    self.assertEqual(accounts.validate_request(request, False)["password"], password)
                else:
                    with self.assertRaises(accounts.RequestError):
                        accounts.validate_request(request, False)

    def test_normal_account_creation_requires_completed_setup(self):
        operations = FakeOperations()
        with self.assertRaisesRegex(accounts.RequestError, "unavailable before setup"):
            accounts.process_request(
                normal_request(), self.admin_caller, self.paths, operations
            )
        self.assertEqual(operations.create_count, 0)

    def test_existing_home_cannot_be_claimed_as_initial_administrator(self):
        operations = FakeOperations()
        operations.homes["alice"] = {"userName": "alice", "memberOf": ["wheel"]}
        with self.assertRaisesRegex(accounts.RequestError, "already exists"):
            accounts.process_request(
                setup_request(), self.setup_caller, self.paths, operations
            )
        self.assertEqual(operations.create_count, 0)
        self.assertFalse(self.paths.state.exists())

    def test_corrupt_recovery_state_fails_closed(self):
        self.paths.state.parent.mkdir(parents=True)
        operations = FakeOperations()
        for contents in ('{', '{"version":1,"phase":"unknown"}'):
            with self.subTest(contents=contents):
                self.paths.state.write_text(contents, encoding="utf-8")
                with self.assertRaises(accounts.RequestError):
                    accounts.process_request(
                        setup_request(), self.setup_caller, self.paths, operations
                    )
        self.assertEqual(operations.create_count, 0)
        self.assertFalse(self.paths.marker.exists())

    def test_setup_caller_cannot_select_normal_admin_creation(self):
        operations = FakeOperations()
        with self.assertRaisesRegex(accounts.RequestError, "only perform initial setup"):
            accounts.process_request(
                setup_request(firstboot=False), self.setup_caller, self.paths, operations
            )
        self.assertEqual(operations.create_count, 0)

    def test_setup_caller_is_denied_after_completion_regardless_of_flags(self):
        self.paths.marker.parent.mkdir(parents=True)
        self.paths.marker.write_text("complete\n", encoding="utf-8")
        for firstboot in (True, False):
            with self.subTest(firstboot=firstboot), self.assertRaisesRegex(
                accounts.RequestError, "already complete"
            ):
                accounts.process_request(
                    setup_request(firstboot=firstboot),
                    self.setup_caller,
                    self.paths,
                    FakeOperations(),
                )

    def test_concurrent_setup_requests_create_only_one_administrator(self):
        operations = FakeOperations(create_delay=0.05)
        outcomes = []

        def run_request():
            try:
                outcomes.append(
                    accounts.process_request(
                        setup_request(), self.setup_caller, self.paths, operations
                    )
                )
            except Exception as error:
                outcomes.append(error)

        threads = [threading.Thread(target=run_request) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(operations.create_count, 1)
        self.assertEqual(sum(isinstance(value, accounts.RequestResult) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, accounts.RequestError) for value in outcomes), 1)

    def test_partial_failure_reuses_created_admin_and_can_finish(self):
        operations = FakeOperations(configure_failures=1)
        with self.assertRaisesRegex(OSError, "injected"):
            accounts.process_request(
                setup_request(), self.setup_caller, self.paths, operations
            )
        self.assertFalse(self.paths.marker.exists())
        self.assertEqual(operations.create_count, 1)

        with self.assertRaisesRegex(accounts.RequestError, "different administrator"):
            accounts.process_request(
                setup_request(username="mallory"),
                self.setup_caller,
                self.paths,
                operations,
            )

        result = accounts.process_request(
            setup_request(), self.setup_caller, self.paths, operations
        )
        self.assertTrue(result.initial_setup)
        self.assertEqual(operations.create_count, 1)
        self.assertTrue(self.paths.marker.exists())
        self.assertTrue(self.paths.disabled_marker.exists())
        self.assertEqual(operations.authenticate_count, 1)

    def test_retry_can_correct_machine_settings_after_authentication(self):
        operations = FakeOperations()
        attempts = []

        def configure(data):
            attempts.append(dict(data))
            if data["keymap"] == "nonexistent-layout":
                raise OSError("invalid keymap")

        operations.configure_machine = configure
        request = setup_request()
        request["keymap"] = "nonexistent-layout"
        with self.assertRaisesRegex(OSError, "invalid keymap"):
            accounts.process_request(request, self.setup_caller, self.paths, operations)
        original_state = self.paths.state.read_bytes()

        corrected = setup_request()
        corrected.update(hostname="corrected-host", locale="en_NZ.UTF-8", timezone="UTC")
        wrong_password = dict(corrected, password="a different password")
        with self.assertRaises(accounts.RequestError):
            accounts.process_request(wrong_password, self.setup_caller, self.paths, operations)
        self.assertEqual(self.paths.state.read_bytes(), original_state)
        self.assertEqual(len(attempts), 1)

        result = accounts.process_request(corrected, self.setup_caller, self.paths, operations)
        self.assertTrue(result.initial_setup)
        self.assertTrue(self.paths.marker.exists())
        self.assertEqual(operations.create_count, 1)
        persisted = accounts._load_state(self.paths)["configuration"]
        for key in ("hostname", "locale", "timezone", "keymap"):
            self.assertEqual(attempts[-1][key], corrected[key])
            self.assertEqual(persisted[key], corrected[key])

    def test_partial_failure_rejects_changed_password(self):
        operations = FakeOperations(configure_failures=1)
        with self.assertRaisesRegex(OSError, "injected"):
            accounts.process_request(
                setup_request(), self.setup_caller, self.paths, operations
            )

        changed = setup_request()
        changed["password"] = "a different retry password"
        with self.assertRaisesRegex(accounts.RequestError, "does not unlock"):
            accounts.process_request(
                changed, self.setup_caller, self.paths, operations
            )

        self.assertEqual(operations.create_count, 1)
        self.assertEqual(operations.configure_count, 1)
        self.assertFalse(self.paths.marker.exists())

        result = accounts.process_request(
            setup_request(), self.setup_caller, self.paths, operations
        )
        self.assertTrue(result.initial_setup)
        self.assertEqual(operations.create_count, 1)

    def test_retirement_failure_is_safe_and_recoverable(self):
        operations = FakeOperations(retirement_failures=1)
        result = accounts.process_request(
            setup_request(), self.setup_caller, self.paths, operations
        )
        self.assertTrue(result.retirement_pending)
        self.assertTrue(self.paths.marker.exists())
        self.assertFalse(self.paths.disabled_marker.exists())
        state = accounts._load_state(self.paths)
        self.assertEqual(state["phase"], "retirement-pending")

        with self.assertRaisesRegex(accounts.RequestError, "already complete"):
            accounts.process_request(
                setup_request(), self.setup_caller, self.paths, operations
            )

        # This is the same idempotent operation run by the enabled path unit.
        operations.retire_setup_account(self.paths)
        self.assertTrue(self.paths.disabled_marker.exists())

    def test_final_state_failure_reports_committed_account_and_schedules_sign_in(self):
        operations = FakeOperations()
        write_state = accounts._write_state

        def fail_completed_state(paths, state):
            if state["phase"] == "complete":
                raise OSError("injected final state-write failure")
            write_state(paths, state)

        with mock.patch.object(accounts, "_write_state", side_effect=fail_completed_state):
            status, response = self.run_setup_helper(operations)

        self.assertEqual(status, 0)
        self.assertEqual(response["event"], "complete")
        self.assertTrue(response["ok"])
        self.assertEqual(operations.schedule_count, 1)
        self.assertEqual(operations.create_count, 1)
        self.assertTrue(self.paths.marker.exists())
        self.assertTrue(self.paths.disabled_marker.exists())
        self.assertEqual(accounts._load_state(self.paths)["phase"], "machine-configured")
        with self.assertRaisesRegex(accounts.RequestError, "already complete"):
            accounts.process_request(setup_request(), self.setup_caller, self.paths, operations)

        with mock.patch.object(accounts.os, "geteuid", return_value=0), \
             mock.patch.object(accounts, "SystemOperations", return_value=operations):
            self.assertEqual(accounts._retire_from_systemd(self.paths), 0)
        self.assertEqual(accounts._load_state(self.paths)["phase"], "complete")

    def test_retirement_and_final_state_failure_are_repaired_by_systemd(self):
        operations = FakeOperations(retirement_failures=1)
        write_state = accounts._write_state

        def fail_retirement_state(paths, state):
            if state["phase"] == "retirement-pending":
                raise OSError("injected retirement state-write failure")
            write_state(paths, state)

        with mock.patch.object(accounts, "_write_state", side_effect=fail_retirement_state):
            status, response = self.run_setup_helper(operations)

        self.assertEqual(status, 0)
        self.assertEqual(response["event"], "complete")
        self.assertIn("automatically", response["warning"])
        self.assertEqual(operations.schedule_count, 1)
        self.assertTrue(self.paths.marker.exists())
        self.assertFalse(self.paths.disabled_marker.exists())
        with mock.patch.object(accounts.os, "geteuid", return_value=0), \
             mock.patch.object(accounts, "SystemOperations", return_value=operations):
            self.assertEqual(accounts._retire_from_systemd(self.paths), 0)
        self.assertTrue(self.paths.disabled_marker.exists())
        self.assertEqual(accounts._load_state(self.paths)["phase"], "complete")

    def test_marker_flush_failure_after_replace_still_finishes_setup(self):
        operations = FakeOperations()
        atomic_write = accounts._atomic_write

        def fail_marker_flush(path, contents, mode=0o644):
            atomic_write(path, contents, mode)
            if path == self.paths.marker:
                raise OSError("injected directory fsync failure")

        with mock.patch.object(accounts, "_atomic_write", side_effect=fail_marker_flush):
            status, response = self.run_setup_helper(operations)

        self.assertEqual(status, 0)
        self.assertEqual(response["event"], "complete")
        self.assertTrue(self.paths.disabled_marker.exists())
        self.assertEqual(operations.schedule_count, 1)

    def test_marker_failure_before_replace_remains_retryable(self):
        operations = FakeOperations()
        atomic_write = accounts._atomic_write

        def fail_marker_write(path, contents, mode=0o644):
            if path == self.paths.marker:
                raise OSError("injected marker write failure")
            atomic_write(path, contents, mode)

        with mock.patch.object(accounts, "_atomic_write", side_effect=fail_marker_write):
            status, response = self.run_setup_helper(operations)

        self.assertEqual(status, 1)
        self.assertEqual(response["event"], "error")
        self.assertFalse(self.paths.marker.exists())
        self.assertEqual(operations.schedule_count, 0)
        status, response = self.run_setup_helper(operations)
        self.assertEqual(status, 0)
        self.assertEqual(response["event"], "complete")
        self.assertEqual(operations.create_count, 1)
        self.assertEqual(operations.schedule_count, 1)

    def test_session_scheduling_failure_reports_success_with_restart_guidance(self):
        operations = FakeOperations()
        with mock.patch.object(operations, "schedule_session_termination", side_effect=OSError("timer unavailable")) as schedule:
            status, response = self.run_setup_helper(operations)

        self.assertEqual(status, 0)
        self.assertEqual(response["event"], "complete")
        self.assertTrue(response["ok"])
        self.assertIn("Restart this computer to sign in", response["warning"])
        self.assertTrue(self.paths.marker.exists())
        schedule.assert_called_once()

    def test_authenticated_administrator_can_create_normal_account(self):
        self.paths.marker.parent.mkdir(parents=True)
        self.paths.marker.write_text("complete\n", encoding="utf-8")
        operations = FakeOperations()
        result = accounts.process_request(
            normal_request(), self.admin_caller, self.paths, operations
        )
        self.assertFalse(result.initial_setup)
        self.assertEqual(operations.create_count, 1)
        self.assertEqual(operations.homes["bob"]["memberOf"], ["wheel"])

    def test_non_admin_and_firstboot_claim_are_denied_after_setup(self):
        self.paths.marker.parent.mkdir(parents=True)
        self.paths.marker.write_text("complete\n", encoding="utf-8")
        with self.assertRaisesRegex(accounts.RequestError, "not an administrator"):
            accounts.process_request(
                normal_request(), accounts.Caller(1002, "guest", False), self.paths, FakeOperations()
            )
        with self.assertRaisesRegex(accounts.RequestError, "only be performed"):
            accounts.process_request(
                setup_request(), self.admin_caller, self.paths, FakeOperations()
            )

    def test_firstboot_account_is_always_an_administrator(self):
        request = setup_request()
        request["admin"] = False
        self.assertTrue(accounts.validate_request(request, True)["admin"])

    def test_invalid_username_and_timezone_are_rejected(self):
        request = setup_request(username="Root User")
        request["timezone"] = "../../etc"
        with self.assertRaises(accounts.RequestError):
            accounts.validate_request(request, True)
        request["username"] = "valid-user"
        with self.assertRaises(accounts.RequestError):
            accounts.validate_request(request, True)

    def test_password_line_separators_are_rejected(self):
        request = normal_request()
        request["password"] = "valid-prefix\ntruncated-suffix"
        with self.assertRaises(accounts.RequestError):
            accounts.validate_request(request, False)


if __name__ == "__main__":
    unittest.main()
