"""Check account-management authorization and the desktop's real IPC contract."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import dbus

from mertensia.accounts import backend, client
from mertensia.accounts.state import LifecyclePaths


class ManagementOperations:
    def __init__(self):
        self.records = {"alice": {"memberOf": ["wheel", "audio"]}, "bob": {"memberOf": []}}
        self.busy = False
        self.options = []
        self.manager = SimpleNamespace(RemoveHome=mock.Mock())

    def home_record(self, username):
        return self.records.get(username)

    def home_names(self):
        return list(self.records)

    def home_in_use(self, username):
        return self.busy

    def update_properties(self, username, options):
        self.options.append((username, options))
        if any(option.startswith("--member-of=") for option in options):
            self.records[username]["memberOf"] = [option.split("=", 1)[1] for option in options if option.split("=", 1)[1]]
        if "--locked=yes" in options:
            self.records[username]["locked"] = True
        if "--locked=no" in options:
            self.records[username]["locked"] = False


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = LifecyclePaths(marker=root / "complete", disabled_marker=root / "disabled",
                                    state=root / "state", lock=root / "lock", gdm_config=root / "gdm")
        self.paths.marker.touch()
        self.operations = ManagementOperations()
        self.admin = backend.Caller(1000, "alice", True)

    def request(self, operation, username="bob", **values):
        return {"operation": operation, "username": username, **values}

    def process(self, request, caller=None):
        return backend.process_request(request, caller or self.admin, self.paths, self.operations)

    def test_non_admin_and_setup_callers_cannot_manage_other_accounts(self):
        for caller in (backend.Caller(1001, "bob", False), backend.Caller(999, "mertensia-setup", False)):
            for request in (self.request("set-admin", enabled=True), self.request("set-locked", enabled=True),
                            self.request("remove", confirm_username="bob")):
                with self.subTest(caller=caller, operation=request["operation"]):
                    with self.assertRaises(backend.RequestError):
                        self.process(request, caller)
        self.assertEqual(self.operations.options, [])
        self.operations.manager.RemoveHome.assert_not_called()

    def test_last_enabled_administrator_cannot_be_demoted_or_disabled(self):
        for operation, enabled in (("set-admin", False), ("set-locked", True)):
            with self.subTest(operation=operation), self.assertRaises(backend.RequestError):
                self.process(self.request(operation, "alice", enabled=enabled))
        self.operations.records["bob"] = {"memberOf": ["wheel"], "locked": True}
        with self.assertRaisesRegex(backend.RequestError, "enabled administrator"):
            self.process(self.request("set-admin", "alice", enabled=False))
        self.assertEqual(self.operations.options, [])

    def test_role_changes_preserve_unrelated_groups(self):
        self.operations.records["bob"]["memberOf"] = ["wheel"]
        self.process(self.request("set-admin", "alice", enabled=False))
        self.assertEqual(self.operations.options, [("alice", ["--member-of=audio"])])

    def test_admin_can_enable_and_promote_another_user(self):
        self.process(self.request("set-admin", enabled=True))
        self.process(self.request("set-locked", enabled=False))
        self.assertEqual(self.operations.options, [("bob", ["--member-of=wheel"]), ("bob", ["--locked=no"])])

    def test_concurrent_demotions_keep_an_enabled_administrator(self):
        self.operations.records["bob"]["memberOf"] = ["wheel"]
        failures = []
        def demote(username):
            try:
                self.process(self.request("set-admin", username, enabled=False))
            except backend.RequestError as error:
                failures.append(str(error))
        threads = [threading.Thread(target=demote, args=(username,)) for username in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.operations.options), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("enabled administrator", failures[0])

    def test_removal_requires_exact_confirmation_and_an_inactive_account(self):
        for confirmation, busy in (("", False), ("alice", False), ("bob", True)):
            self.operations.busy = busy
            with self.subTest(confirmation=confirmation, busy=busy), self.assertRaises(backend.RequestError):
                self.process(self.request("remove", confirm_username=confirmation))
        self.operations.manager.RemoveHome.assert_not_called()
        self.operations.busy = False
        self.process(self.request("remove", confirm_username="bob"))
        self.operations.manager.RemoveHome.assert_called_once_with("bob", timeout=120)

    def test_current_account_cannot_be_removed_or_disabled(self):
        self.operations.records["bob"]["memberOf"] = ["wheel"]
        for request in (self.request("remove", "alice", confirm_username="alice"),
                        self.request("set-locked", "alice", enabled=True)):
            with self.assertRaisesRegex(backend.RequestError, "account you are using"):
                self.process(request)

    def test_invalid_operations_targets_and_boolean_types_are_rejected(self):
        for request in (self.request("set-admin", enabled="yes"), self.request("unsupported"),
                        self.request("remove", "--help"), self.request("remove", "missing")):
            with self.subTest(request=request), self.assertRaises(backend.RequestError):
                self.process(request)


class AccountsClientTests(unittest.TestCase):
    def setUp(self):
        self.home = mock.Mock()
        self.client = client.AccountsClient.__new__(client.AccountsClient)
        self.client.home = self.home
        identity = mock.patch.object(client, "current_identity", return_value=client.Identity("alice", 1000, False))
        identity.start()
        self.addCleanup(identity.stop)

    def test_listing_uses_resolved_profile_properties_and_filters_system_accounts(self):
        self.client.bus = mock.Mock()
        self.home.ListHomes.return_value = [
            ("system", 999, "inactive", 999, "", "", "", "/system"),
            ("alice", 1000, "active", 1000, "Fallback", "", "", "/alice"),
        ]
        login, service, properties = mock.Mock(), mock.Mock(), mock.Mock()
        login.ListSessions.return_value = [("session", 1000, "alice", "seat0", "/session")]
        service.FindUserByName.return_value = "/alice"
        properties.GetAll.return_value = {"RealName": "Alice Morgan", "AccountType": 1, "Locked": False,
                                         "Language": "fr_FR.utf8", "IconFile": "/var/lib/home/alice/avatar"}
        with mock.patch.object(client.dbus, "Interface", side_effect=[login, service, properties]):
            accounts = self.client.accounts()
        self.assertEqual(accounts, [client.Account("alice", 1000, "Alice Morgan", True, False, True,
                                                   "fr_FR.utf8", "/var/lib/home/alice/avatar", "active")])
        service.FindUserByName.assert_called_once_with("alice", timeout=30)

    def test_password_change_uses_home1_with_new_and_old_secrets_in_the_correct_order(self):
        self.client.change_password("alice", "old password", 'new \\" λ password')
        args = self.home.ChangePasswordHome.call_args.args
        self.assertEqual(args[0], "alice")
        self.assertEqual(json.loads(args[1]), {"password": ['new \\" λ password']})
        self.assertEqual(json.loads(args[2]), {"password": ["old password"]})

    def test_password_changes_cannot_target_someone_else_or_bypass_validation(self):
        for username, current, new in (("bob", "old password", "new password"),
                                       ("alice", "", "new password"), ("alice", "old password", "short")):
            with self.subTest(username=username, current=current, new=new), self.assertRaises(client.RequestError):
                self.client.change_password(username, current, new)
        self.home.ChangePasswordHome.assert_not_called()

    def test_authentication_errors_are_actionable_and_do_not_echo_untrusted_service_messages(self):
        error = dbus.DBusException("sensitive backend context", name="org.freedesktop.home1.BadPassword")
        self.assertIn("current password is incorrect", client.error_message(error))
        self.assertNotIn("sensitive", client.error_message(error))

    def test_admin_helper_receives_secrets_on_stdin_only(self):
        result = SimpleNamespace(stdout='{"event":"complete","ok":true}\n', returncode=0)
        with mock.patch.object(client.subprocess, "run", return_value=result) as run:
            client.administrator_request({"password": "example secret"})
        self.assertNotIn("example secret", " ".join(run.call_args.args[0]))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["password"], "example secret")

    def test_cancelled_admin_authorization_has_a_retry_message(self):
        with mock.patch.object(client.subprocess, "run", return_value=SimpleNamespace(stdout="", returncode=126)):
            with self.assertRaisesRegex(client.RequestError, "cancelled"):
                client.administrator_request({"operation": "remove", "username": "bob"})
