"""Run the actual PAM conversation/module against private, non-mutating stacks."""
import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from mertensia.accounts import passwords


ROOT = Path(__file__).resolve().parents[2]


class PasswordPamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        # The override permits local checks with public upstream headers when
        # pam-devel is unavailable. CI uses Fedora's installed pam-devel.
        headers = os.environ.get("MERTENSIA_PAM_INCLUDE_DIR", "/usr/include")
        for source, name in (("system/pam/pam_mertensia_password.c", "tokens.so"),
                             ("tests/fixtures/password-pam.c", "fixture.so")):
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                            "-I" + headers, str(ROOT / source), "-Wl,-l:libpam.so.0", "-o", str(cls.root / name)],
                           check=True, capture_output=True, env=dict(os.environ, CCACHE_DISABLE="1"))

    def setUp(self):
        self.testdir = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.testdir.cleanup)
        self.directory = Path(self.testdir.name)
        self.marker = self.directory / "calls"
        self.pam = passwords._libpam()
        self.pam.pam_start_confdir.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                              ctypes.POINTER(passwords.Conversation), ctypes.c_char_p,
                                              ctypes.POINTER(ctypes.c_void_p)]

    def stack(self, home="home", keyring=True):
        lines = [f"password requisite {self.root / 'tokens.so'}",
                 f"password [success=ok default=die] {self.root / 'fixture.so'} {home} {self.marker}"]
        if keyring:
            lines.append(f"password optional {self.root / 'fixture.so'} keyring {self.marker}")
        (self.directory / "mertensia-password").write_text("\n".join(lines) + "\n")

    def change(self, current="old λ password", password="new λ password"):
        def start(service, username, conversation, handle):
            self.assertEqual(service, b"mertensia-password")
            return self.pam.pam_start_confdir(service, username, conversation, os.fsencode(self.directory), handle)
        adapter = SimpleNamespace(pam_start=start, pam_chauthtok=self.pam.pam_chauthtok, pam_end=self.pam.pam_end)
        with mock.patch.object(passwords, "_libpam", return_value=adapter):
            passwords.change_password("alice", current, password)

    def test_both_tokens_reach_home_then_keyring_with_unicode_intact(self):
        self.stack()
        self.change()
        self.assertEqual(self.marker.read_text().splitlines(), ["home", "keyring"])

    def test_wrong_current_password_cannot_reach_keyring(self):
        self.stack()
        with self.assertRaises(passwords.PamError):
            self.change(current="wrong password")
        self.assertFalse(self.marker.exists())

    def test_failed_home_change_cannot_reach_keyring(self):
        self.stack(home="failed-home")
        with self.assertRaises(passwords.PamError):
            self.change()
        self.assertEqual(self.marker.read_text().splitlines(), ["failed-home"])

    def test_missing_optional_keyring_module_does_not_undo_home_change(self):
        self.stack(keyring=False)
        with (self.directory / "mertensia-password").open("a") as stream:
            stream.write("password optional /nonexistent/pam_keyring.so\n")
        self.change()
        self.assertEqual(self.marker.read_text().splitlines(), ["home"])

    def test_ignored_home_change_fails_closed_before_keyring(self):
        self.stack(home="ignored-home")
        with self.assertRaises(passwords.PamError):
            self.change()
        self.assertEqual(self.marker.read_text().splitlines(), ["ignored-home"])

    def test_missing_service_fails_closed(self):
        with self.assertRaises(passwords.PamError):
            self.change()
        self.assertFalse(self.marker.exists())

    def test_unknown_or_repeated_prompt_is_rejected_without_a_response(self):
        libc = passwords._libc()
        converse = passwords._conversation("old", "new", libc)
        response = ctypes.POINTER(passwords.Response)()
        message = passwords.Message(1, b"Unexpected challenge")
        messages = (ctypes.POINTER(passwords.Message) * 1)(ctypes.pointer(message))
        self.assertEqual(converse(1, messages, ctypes.byref(response), None), passwords.PAM_CONV_ERR)
        self.assertFalse(response)
        message.text = b"Current password: "
        self.assertEqual(converse(1, messages, ctypes.byref(response), None), passwords.PAM_SUCCESS)
        self.assertEqual(ctypes.string_at(response[0].text), b"old")
        libc.explicit_bzero(response[0].text, 3)
        libc.free(response[0].text)
        libc.free(response)
        self.assertEqual(converse(1, messages, ctypes.byref(response), None), passwords.PAM_CONV_ERR)
        self.assertFalse(response)
