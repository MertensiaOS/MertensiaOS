import io
import unittest
from unittest import mock


from mertensia.accounts import login_users

class LoginUserTests(unittest.TestCase):
    def setUp(self):
        self.homes = mock.Mock()
        self.accounts = mock.Mock()
        self.accounts.CacheUser.side_effect = lambda username: f"/accounts/{username}"
        self.accounts.ListCachedUsers.return_value = ["/accounts/alice", "/accounts/bob"]
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
        self.homes.ListHomes.return_value = [("alice", 60001, "inactive"), ("bob", 60002, "inactive")]
        self.accounts.CacheUser.side_effect = [login_users.dbus.DBusException("not ready"), "/accounts/bob"]
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
            self.homes.ListHomes.return_value = [("alice", 60001, "inactive")]
            self.assertEqual(login_users.sync_users(self.bus), 0)
            self.assertEqual(login_users.sync_users(self.bus), 0)
        self.assertEqual(self.accounts.CacheUser.call_args_list, [mock.call("alice"), mock.call("alice")])

    def test_successful_cache_call_with_empty_login_list_requests_retry(self):
        # AccountsService's homed CacheUser implementation can succeed without
        # promoting the user into ListCachedUsers (the 50 shadow-entry bug).
        self.homes.ListHomes.return_value = [("alice", 60001, "inactive")]
        self.accounts.ListCachedUsers.return_value = []
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(login_users.sync_users(self.bus), 1)
        self.assertIn("alice is absent", errors.getvalue())

    def test_absent_home_does_not_trigger_cache_calls_or_endless_retries(self):
        self.homes.ListHomes.return_value = [("alice", 60001, "absent"), ("bob", 60002, "inactive")]
        self.accounts.ListCachedUsers.return_value = ["/accounts/bob"]
        self.assertEqual(login_users.sync_users(self.bus), 0)
        self.accounts.CacheUser.assert_called_once_with("bob")


if __name__ == "__main__":
    unittest.main()
