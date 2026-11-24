import ctypes
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from keyring.errors import KeyringError, KeyringLocked

from maildesk.service import CredentialAccessPaused, KeyringVault, MailboxAuthorizationRequired, MailService
from tests.test_service import FakeVault


def osstatus(code):
    error = KeyringError("simulated Security.framework result")
    error.__cause__ = OSError(code, "SecKeychain")
    return error


class MailboxAccessTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.vault = KeyringVault()
        for change in (
            patch.object(self.vault, "_backend", return_value=self.backend),
            patch.object(KeyringVault, "_session_secrets", {}),
            patch.object(KeyringVault, "_paused", Event()),
            patch("maildesk.service.sys.platform", "darwin"),
        ):
            change.start()
            self.addCleanup(change.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "mail.db"
        fixture = MailService(path, Mock(), FakeVault())
        self.qq = fixture.bind("qq", "test@qq.com", "sample-qq")
        self.other = fixture.bind("163", "test@163.com", "sample-163")
        self.service = MailService(path, Mock(), self.vault)
        self.path = path

    def test_session_only_legacy_grant_does_not_get_mistaken_for_persistent_access(self):
        trusted = {self.other.address}
        this_session = set()

        def read_silently(service, address):
            if address not in trusted and address not in this_session:
                raise osstatus(-25308)
            return "sample-token"

        def allow(service, address):
            this_session.add(address)
            return "sample-token"

        self.backend.get_password_without_ui.side_effect = read_silently
        self.backend.get_password.side_effect = allow
        self.backend.set_password.side_effect = lambda _, reference, value: trusted.add(reference)
        with self.assertRaises(MailboxAuthorizationRequired):
            self.service.poll_pending(self.qq)
        self.service.list_messages(self.other, 5)
        self.backend.get_password.assert_not_called()
        self.assertFalse(KeyringVault._paused.is_set())
        self.service.authorize_mailbox(self.qq)
        self.service.list_messages(self.qq, 5)
        self.backend.get_password.assert_called_once_with(self.vault.service_name, self.qq.address)
        self.assertNotEqual(self.service._require_bound(self.qq)[0], self.qq.address)

        # A new process has no cached secrets; OS permissions are independent.
        this_session.clear()
        with patch.object(KeyringVault, "_session_secrets", {}):
            self.service.list_messages(self.qq, 5)
            self.service.list_messages(self.other, 5)
        self.assertEqual(self.backend.get_password.call_count, 1)

    def test_denial_blocks_more_dialogs_but_another_silently_readable_account_syncs(self):
        self.backend.get_password.side_effect = KeyringLocked()
        self.backend.get_password_without_ui.side_effect = [
            osstatus(-25308), osstatus(-25308), osstatus(-25315), "sample-token",
        ]
        with self.assertRaises(CredentialAccessPaused):
            self.service.authorize_mailbox(self.qq)
        with self.assertRaises(CredentialAccessPaused):
            self.service.authorize_mailbox(self.qq)
        with self.assertRaises(MailboxAuthorizationRequired):
            self.service.poll_pending(self.qq)
        self.service.list_messages(self.other, 5)
        self.backend.get_password.assert_called_once()

    def test_missing_or_broken_entry_never_falls_back_to_an_interactive_read(self):
        self.backend.get_password_without_ui.side_effect = [None, osstatus(-50)]
        with self.assertRaisesRegex(ValueError, "重新绑定"):
            self.service.list_messages(self.qq, 5)
        with self.assertRaises(KeyringError):
            self.service.list_messages(self.qq, 5)
        self.backend.get_password.assert_not_called()

    def legacy_store(self):
        values = {self.qq.address: "legacy-credential-secret"}

        def passive(service, reference):
            if reference == self.qq.address:
                raise osstatus(-25308)
            return values.get(reference)

        self.backend.get_password.side_effect = lambda _, key: values.get(key)
        self.backend.get_password_without_ui.side_effect = passive
        self.backend.set_password.side_effect = lambda _, key, secret: values.update({key: secret})
        self.backend.delete_password_without_ui.side_effect = lambda _, key: values.pop(key)
        return values

    def test_explicit_legacy_migration_persists_only_verified_reference_and_rebind_updates_it(self):
        values = self.legacy_store()
        self.service.authorize_mailbox(self.qq)
        self.backend.get_password.assert_called_once_with(self.vault.service_name, self.qq.address)
        ref, _ = self.service._require_bound(self.qq)
        self.assertNotEqual(ref, self.qq.address)
        self.assertEqual(values, {ref: "legacy-credential-secret", self.qq.address: "legacy-credential-secret"})
        self.assertNotIn(b"legacy-credential-secret", self.path.read_bytes())
        with patch.object(KeyringVault, "_session_secrets", {}):
            fresh = MailService(self.path, Mock(), self.vault)
            fresh.list_messages(self.qq, 4)
            self.backend.get_password_without_ui.assert_called_with(self.vault.service_name, ref)
            self.assertEqual(self.backend.get_password.call_count, 1)
            del values[ref]
            fresh.bind("qq", self.qq.address, "new-credential-secret")
            self.assertEqual(values, {ref: "new-credential-secret", self.qq.address: "legacy-credential-secret"})
            self.assertEqual(fresh._require_bound(self.qq)[0], ref)
            self.assertNotIn(b"new-credential-secret", self.path.read_bytes())

    def test_failure_to_verify_new_item_keeps_legacy_reference_and_removes_copy(self):
        values = self.legacy_store()
        self.backend.get_password_without_ui.side_effect = osstatus(-25308)
        with self.assertRaises(MailboxAuthorizationRequired):
            self.service.authorize_mailbox(self.qq)
        self.assertEqual(values, {self.qq.address: "legacy-credential-secret"})
        self.assertEqual(self.service._require_bound(self.qq)[0], self.qq.address)

    def test_rebind_while_authorizing_keeps_newer_reference_and_rolls_back_copy(self):
        values = self.legacy_store()
        write = self.backend.set_password.side_effect

        def concurrent_change(service, key, secret):
            write(service, key, secret)
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE mailbox_key_refs SET revision=revision+1 WHERE address=?", (self.qq.address,))

        self.backend.set_password.side_effect = concurrent_change
        with self.assertRaisesRegex(RuntimeError, "另一处更新"):
            self.service.authorize_mailbox(self.qq)
        self.assertEqual(values, {self.qq.address: "legacy-credential-secret"})
        self.assertEqual(self.service._require_bound(self.qq)[0], self.qq.address)


@unittest.skipUnless(sys.platform == "darwin", "uses macOS Security.framework types")
class QuietNativeReadTests(unittest.TestCase):
    def setUp(self):
        from maildesk import mac_keychain
        self.backend = mac_keychain.MacKeyring()
        self.allowed = True
        for name in ("_get_interaction", "_set_interaction", "_find_password", "_free_content"):
            change = patch.object(mac_keychain, name, return_value=0)
            setattr(self, name, change.start())
            self.addCleanup(change.stop)

        def current(ptr):
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte))[0] = self.allowed
            return 0

        def set_current(allowed):
            self.allowed = bool(allowed)
            return 0

        self._get_interaction.side_effect = current
        self._set_interaction.side_effect = set_current

    def test_restores_previous_state_and_releases_the_native_data(self):
        token = ctypes.create_string_buffer("授权码".encode())

        def found(_, slen, service, ulen, username, length, data, item):
            self.assertFalse(self.allowed)
            self.assertEqual(service[:slen], b"fixture")
            self.assertEqual(username[:ulen], b"test@qq.com")
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(token.value)
            ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(token)
            return 0

        self._find_password.side_effect = found
        self.assertEqual(self.backend.get_password_without_ui("fixture", "test@qq.com"), "授权码")
        self.assertTrue(self.allowed)
        self._free_content.assert_called_once()

        self._find_password.side_effect = None
        self._find_password.return_value = -25308
        with self.assertRaises(KeyringError):
            self.backend.get_password_without_ui("fixture", "test@qq.com")
        self.assertTrue(self.allowed)

    def test_does_not_enable_ui_that_was_already_suppressed(self):
        self.allowed = False
        self._find_password.return_value = -25300
        self.assertIsNone(self.backend.get_password_without_ui("fixture", "test@qq.com"))
        self._set_interaction.assert_not_called()

    def test_failure_to_disable_prompts_stops_before_querying_a_password(self):
        self._set_interaction.side_effect = None
        self._set_interaction.return_value = -50
        with self.assertRaises(KeyringError):
            self.backend.get_password_without_ui("fixture", "test@qq.com")
        self._find_password.assert_not_called()
