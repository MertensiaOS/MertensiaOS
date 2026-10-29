"""Host-side failure cases for guest browser diagnostics."""

import copy
import unittest
from unittest import mock

from integration import chromium_probe as probe, guest


class ChromiumProbeTests(unittest.TestCase):
    def policies(self):
        return {"chromePolicies": {
            name: {"value": value, "level": level, "scope": "machine", "source": "platform"}
            for level, policies in probe.POLICIES.items() for name, value in policies.items()
        }}

    def test_accepts_effective_machine_policies(self):
        self.assertEqual(len(probe.validate_policies(self.policies())), 8)

    def test_rejects_missing_policy_and_ambiguous_exports(self):
        export = self.policies()
        del export["chromePolicies"]["SitePerProcess"]
        for value in (export, [self.policies(), self.policies()]):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                probe.validate_policies(value)

    def test_rejects_wrong_types_values_levels_sources_and_scopes(self):
        for field, value in (("value", 1), ("value", False), ("level", "recommended"),
                             ("scope", "user"), ("source", "cloud")):
            export = self.policies()
            export["chromePolicies"]["SitePerProcess"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                probe.validate_policies(export)

    def test_rejects_errors_ignored_and_unknown_policies(self):
        for field in ("error", "warning", "ignored", "unknown", "deprecated", "conflicts"):
            export = self.policies()
            export["chromePolicies"]["SitePerProcess"][field] = True
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                probe.validate_policies(export)
        export = self.policies()
        export["chromePolicies"]["UnknownPolicy"] = {"error": "Unknown policy"}
        with self.assertRaises(RuntimeError):
            probe.validate_policies(export)

    def test_requires_every_native_sandbox_layer(self):
        status = dict.fromkeys(("userNs", "pidNs", "netNs", "seccompBpf", "seccompTsync"), True)
        status["suid"] = False
        probe.validate_sandbox(status)
        for key in status:
            bad = copy.copy(status)
            bad[key] = not bad[key]
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                probe.validate_sandbox(bad)

    def test_refuses_root_browser(self):
        with mock.patch.object(probe.os, "geteuid", return_value=0), self.assertRaisesRegex(RuntimeError, "unprivileged"):
            probe.probe()

    def test_guest_cleans_disposable_account_after_probe_failure(self):
        commands = []

        def run(argv, **kwargs):
            commands.append(argv)
            if argv[0] == "runuser":
                raise RuntimeError("browser failed")
            return ""

        with mock.patch.object(guest, "role", return_value="payload"), \
             mock.patch.object(guest, "TEST_STATE"), \
             mock.patch.object(guest.subprocess, "run", return_value=mock.Mock(returncode=2)), \
             mock.patch.object(guest, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "browser failed"):
                guest.chromium_checks()
        self.assertEqual(commands[-1], ["userdel", "--remove", "mertensia-browser-test"])
        browser_command = commands[1]
        self.assertIn("-u", browser_command)
        self.assertIn("mertensia-browser-test", browser_command)
        self.assertNotIn("--no-sandbox", browser_command)

    def test_guest_refuses_live_image_and_existing_account(self):
        with mock.patch.object(guest, "role", return_value="live"), mock.patch.object(guest, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "installed system"):
                guest.chromium_checks()
            run.assert_not_called()
        with mock.patch.object(guest, "role", return_value="payload"), \
             mock.patch.object(guest.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(guest, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                guest.chromium_checks()
            run.assert_not_called()
