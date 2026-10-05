import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("login_users", ROOT / "system/mertensia_sync_login_users.py")
login_users = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(login_users)


class LoginUserTests(unittest.TestCase):
    def setUp(self):
        self.homes = mock.Mock()
        self.accounts = mock.Mock()
        self.bus = mock.Mock()
        self.interfaces = mock.patch.object(login_users.dbus, "Interface", side_effect=[self.homes, self.accounts])
        self.interfaces.start()
        self.addCleanup(self.interfaces.stop)

    def test_inactive_and_active_homes_are_cached_without_activation(self):
        self.homes.ListHomes.return_value = [
            ("alice", 60001, "inactive", 60001, "Alice", "/home/alice", "/bin/bash", "/org/freedesktop/home1/home/alice"),
            ("bob", 60002, "active", 60002, "Bob", "/home/bob", "/bin/bash", "/org/freedesktop/home1/home/bob"),
        ]
        self.assertEqual(login_users.sync_users(self.bus), 0)
        self.assertEqual(self.accounts.CacheUser.call_args_list, [mock.call("alice"), mock.call("bob")])
        self.assertEqual(self.homes.method_calls, [mock.call.ListHomes()])

    def test_failure_allows_other_users_to_be_cached_and_requests_retry(self):
        self.homes.ListHomes.return_value = [("alice",), ("bob",)]
        self.accounts.CacheUser.side_effect = [login_users.dbus.DBusException("not ready"), None]
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(login_users.sync_users(self.bus), 1)
        self.accounts.CacheUser.assert_any_call("bob")

    def test_no_homes_does_not_cache_the_temporary_setup_account(self):
        self.homes.ListHomes.return_value = []
        self.assertEqual(login_users.sync_users(self.bus), 0)
        self.accounts.CacheUser.assert_not_called()

    def test_repeated_sync_is_safe(self):
        self.interfaces.stop()
        with mock.patch.object(login_users.dbus, "Interface", side_effect=[self.homes, self.accounts] * 2):
            self.homes.ListHomes.return_value = [("alice",)]
            self.assertEqual(login_users.sync_users(self.bus), 0)
            self.assertEqual(login_users.sync_users(self.bus), 0)
        self.assertEqual(self.accounts.CacheUser.call_args_list, [mock.call("alice"), mock.call("alice")])


if __name__ == "__main__":
    unittest.main()
