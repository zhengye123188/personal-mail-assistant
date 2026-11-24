import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from keyring.errors import KeyringError, KeyringLocked

from maildesk.models import Account, MailItem, MailPage
from maildesk.providers import provider_for_address
from maildesk.service import CredentialAccessPaused, KeyringVault, MailService


class FakeVault:
    def __init__(self):
        self.values = {}
        self.fail_next_write = False

    def get(self, address):
        return self.values.get(address)

    def set(self, address, secret):
        self.values[address] = secret
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("系统凭据写入失败")

    def delete(self, address):
        del self.values[address]


class KeyringVaultTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        for override in (patch.object(KeyringVault, "_backend", return_value=self.backend),
                         patch.object(KeyringVault, "_session_secrets", {}),
                         patch.object(KeyringVault, "_paused", Event())):
            override.start()
            self.addCleanup(override.stop)

    def test_concurrent_readers_share_an_authorization_across_instances(self):
        entered, release = Event(), Event()

        def authorize(service, address):
            entered.set()
            self.assertTrue(release.wait(2))
            return "approved"

        self.backend.get_password.side_effect = authorize
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(KeyringVault().get, "alice@qq.com")
            try:
                self.assertTrue(entered.wait(2))
                other = pool.submit(KeyringVault().get, "alice@qq.com")
            finally:
                release.set()
            self.assertEqual(first.result(), "approved")
            self.assertEqual(other.result(), "approved")
        self.assertEqual(KeyringVault().get("alice@qq.com"), "approved")
        self.backend.get_password.assert_called_once_with(KeyringVault.service_name, "alice@qq.com")

    def test_denial_suppresses_other_prompts_until_explicit_retry(self):
        vault = KeyringVault()
        self.backend.get_password.side_effect = ["qq-token", KeyringLocked(), "163-token"]
        self.assertEqual(vault.get("alice@qq.com"), "qq-token")
        with self.assertRaises(CredentialAccessPaused):
            vault.get("alice@163.com")
        with self.assertRaises(CredentialAccessPaused):
            KeyringVault().get("reply-api/deepseek")
        self.assertEqual(KeyringVault().get("alice@qq.com"), "qq-token")
        self.assertEqual(self.backend.get_password.call_count, 2)
        KeyringVault.resume_access()
        self.assertEqual(vault.get("alice@163.com"), "163-token")

    def test_mac_authorization_statuses_pause_other_reads_but_io_errors_do_not(self):
        with patch("maildesk.service.sys.platform", "darwin"):
            for status in (-128, -25293, -25308):
                with self.subTest(status=status):
                    KeyringVault.resume_access()
                    failed = KeyringError()
                    failed.__cause__ = Exception(status, "OSStatus")
                    self.backend.get_password.side_effect = failed
                    with self.assertRaises(CredentialAccessPaused):
                        KeyringVault().get("alice@qq.com")
                    self.backend.get_password.reset_mock()
                    with self.assertRaises(CredentialAccessPaused):
                        KeyringVault().get("alice@163.com")
                    self.backend.get_password.assert_not_called()
            KeyringVault.resume_access()
            self.backend.get_password.side_effect = [KeyringError("transient"), None, "restored"]
            with self.assertRaisesRegex(KeyringError, "transient"):
                KeyringVault().get("alice@qq.com")
            self.assertIsNone(KeyringVault().get("alice@qq.com"))
            self.assertEqual(KeyringVault().get("alice@qq.com"), "restored")

    def test_updates_deletions_and_failed_writes_invalidate_old_values(self):
        vault = KeyringVault()
        addr = "reply-api/deepseek"
        self.backend.get_password.side_effect = ["initial", "after-partial-write", None]
        self.assertEqual(vault.get(addr), "initial")
        vault.set(addr, "updated")
        self.assertEqual(KeyringVault().get(addr), "updated")
        self.backend.set_password.side_effect = KeyringError("partial write")
        with self.assertRaises(KeyringError):
            vault.set(addr, "possible-change")
        self.assertEqual(vault.get(addr), "after-partial-write")
        KeyringVault().delete(addr)
        self.assertIsNone(vault.get(addr))
        self.backend.delete_password.assert_called_once_with(vault.service_name, addr)

    def test_rebinding_compares_the_current_keychain_item_even_after_external_changes(self):
        saved = {}
        self.backend.get_password.side_effect = lambda service, addr: saved.get(addr)
        self.backend.set_password.side_effect = lambda service, addr, value: saved.update({addr: value})
        with tempfile.TemporaryDirectory() as folder:
            service = MailService(Path(folder) / "accounts.sqlite", Mock(), KeyringVault())
            addr = "alice@qq.com"
            service.bind("qq", addr, "token")
            service.bind("qq", addr, "token")
            self.assertEqual(self.backend.set_password.call_count, 1)
            saved[addr] = "externally-rotated"
            service.bind("qq", addr, "token")
            self.assertEqual(saved[addr], "token")
            del saved[addr]
            service.bind("qq", addr, "token")
            self.assertEqual(saved[addr], "token")
            self.assertEqual(self.backend.set_password.call_count, 3)


@unittest.skipUnless(sys.platform == "darwin", "uses system CoreFoundation types")
class MacKeyringWriteTests(unittest.TestCase):
    def setUp(self):
        from maildesk import mac_keychain
        self.mac = mac_keychain
        self.keyring = self.mac.MacKeyring()
        for function in ("_update", "_add"):
            stub = patch.object(self.mac, function, return_value=0)
            setattr(self, function, stub.start())
            self.addCleanup(stub.stop)

    def test_update_targets_the_existing_item_and_changes_only_binary_password_data(self):
        import ctypes as C

        mac = self.mac
        count = mac._function(mac._cf, "CFDictionaryGetCount", C.c_long, C.c_void_p)
        value = mac._function(mac._cf, "CFDictionaryGetValue", C.c_void_p, C.c_void_p, C.c_void_p)
        string = mac._function(mac._cf, "CFStringGetCString", C.c_bool,
                               C.c_void_p, C.c_char_p, C.c_long, C.c_uint32)
        data = mac._function(mac._cf, "CFDataGetBytePtr", C.c_void_p, C.c_void_p)
        length = mac._function(mac._cf, "CFDataGetLength", C.c_long, C.c_void_p)
        password = "新\0token"

        def check(query, updates):
            self.assertEqual(count(query), 3)
            self.assertEqual(value(query, mac._constant("kSecClass")), mac._constant("kSecClassGenericPassword"))
            for field, expected in (("kSecAttrService", "personal-mail-assistant"), ("kSecAttrAccount", "alice@qq.com")):
                buffer = C.create_string_buffer(64)
                self.assertTrue(string(value(query, mac._constant(field)), buffer, len(buffer), mac._utf8))
                self.assertEqual(buffer.value.decode(), expected)
            self.assertEqual(count(updates), 1)
            payload = value(updates, mac._constant("kSecValueData"))
            self.assertEqual(C.string_at(data(payload), length(payload)), password.encode())
            return 0

        self._update.side_effect = check
        self.keyring.set_password("personal-mail-assistant", "alice@qq.com", password)
        self._add.assert_not_called()

    def test_only_missing_items_are_added_and_concurrent_inserts_are_updated(self):
        self._update.side_effect = [-25300, -25300, 0]
        self._add.side_effect = [0, -25299]
        for _ in range(2):
            self.keyring.set_password("test-service", "alice", "new")
        self.assertEqual(self._update.call_count, 3)
        self.assertEqual(self._add.call_count, 2)

    def test_denied_update_never_deletes_or_recreates_existing_credential(self):
        backend = Mock(get_password=Mock(return_value="old"), set_password=self.keyring.set_password)
        addr = "alice@qq.com"
        with tempfile.TemporaryDirectory() as folder, patch.object(KeyringVault, "_backend", return_value=backend), \
                patch.object(KeyringVault, "_session_secrets", {}), patch.object(KeyringVault, "_paused", Event()):
            service = MailService(Path(folder) / "accounts.sqlite", Mock(), KeyringVault())
            account = service.bind("qq", addr, "old")
            self._update.return_value = -128
            with self.assertRaises(CredentialAccessPaused):
                service.bind("qq", addr, "new")
            with self.assertRaises(CredentialAccessPaused):
                KeyringVault().get(addr)
            self.assertEqual(service.accounts(), [account])
            self.assertEqual(backend.get_password.call_count, 2)
            self._update.assert_called_once()
            self._add.assert_not_called()
            backend.delete_password.assert_not_called()
            KeyringVault.resume_access()
            self.assertEqual(KeyringVault().get(addr), "old")


class AccountServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "state" / "accounts.sqlite"
        self.client = Mock()
        self.vault = FakeVault()
        self.service = MailService(self.db_path, self.client, self.vault)

    def test_normalized_account_survives_restart_without_storing_secret_in_sqlite(self):
        secret = "test-value-only-in-vault"
        account = self.service.bind(" QQ ", " ME@QQ.COM ", secret)
        self.assertEqual(account, Account(address="me@qq.com", provider="qq"))
        self.client.verify.assert_called_once_with(account, secret)
        self.assertEqual(self.vault.get(account.address), secret)
        reopened = MailService(self.db_path, self.client, self.vault)
        self.assertEqual(reopened.accounts(), [account])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertNotIn(secret, "\n".join(db.iterdump()))

    def test_validation_happens_before_contacting_mail_provider(self):
        invalid = [
            ("qq", "me@163.com", "ok"),
            ("163", "me@qq.com", "ok"),
            ("other", "me@other.com", "ok"),
            ("qq", "a@b@qq.com", "ok"),
            ("qq", "me@qq.com", "  "),
            ("gmail", "me@gmail.com.example.org", "abcdefghijklmnop"),
            ("gmail", "me@school.example", "abcdefghijklmnop"),
            ("gmail", "me@gmail.com", "too-short"),
            ("icloud", "me@apple.com", "app-password"),
            ("126", "me@vip.126.com", "ok"),
            ("yahoo", "me@yahoo.co.jp", "ok"),
        ]
        for provider, address, secret in invalid:
            with self.subTest(address=address, provider=provider), self.assertRaises(ValueError):
                self.service.bind(provider, address, secret)
        self.client.verify.assert_not_called()
        self.assertEqual(self.service.accounts(), [])

    def test_personal_mail_domains_including_legacy_aliases(self):
        addresses = {
            "qq": ("qq.com",), "163": ("163.com",), "126": ("126.com",), "yeah": ("yeah.net",),
            "gmail": ("gmail.com", "googlemail.com"), "icloud": ("icloud.com", "me.com", "mac.com"),
            "yahoo": ("yahoo.com",),
        }
        for provider, domains in addresses.items():
            for domain in domains:
                with self.subTest(domain=domain):
                    address = f" USER@{domain.upper()} "
                    self.assertEqual(provider_for_address(address), provider)
                    secret = "abcd efgh ijkl mnop" if provider == "gmail" else "Ab12-cd34 ef56"
                    account = self.service.bind(provider, address, secret)
                    expected = "abcdefghijklmnop" if provider == "gmail" else secret
                    self.client.verify.assert_called_with(account, expected)
                    self.assertEqual(self.vault.get(account.address), expected)
        for invalid in ("", "user@", "@gmail.com", "x@y@gmail.com", "user@qq.com.example.org"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(provider_for_address(invalid))

    def test_existing_accounts_and_credentials_work_alongside_new_bindings(self):
        old_path = self.db_path.with_name("existing.sqlite")
        existing = [Account("me@qq.com", "qq"), Account("me@163.com", "163")]
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("CREATE TABLE accounts (address TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL)")
            for account in existing:
                db.execute("INSERT INTO accounts VALUES (?, ?)", (account.address, account.provider))
                self.vault.set(account.address, f"token-{account.provider}")
            db.commit()
        reopened = MailService(old_path, self.client, self.vault)
        gmail = reopened.bind("gmail", "me@gmail.com", "abcd\u00a0efgh\u202fijkl mnop")
        self.assertEqual(reopened.accounts(), [*existing, gmail])
        for account in existing:
            reopened.list_messages(account, 5)
            self.client.list_messages.assert_called_with(account, f"token-{account.provider}", 5)
        self.assertEqual(self.vault.get(gmail.address), "abcdefghijklmnop")

    def test_rebind_verification_failure_preserves_old_credentials(self):
        account = self.service.bind("163", "me@163.com", "old")
        self.client.verify.side_effect = RuntimeError("远程认证失败")
        with self.assertRaises(RuntimeError):
            self.service.bind("163", account.address, "wrong")
        self.assertEqual(self.vault.get(account.address), "old")
        self.client.verify.side_effect = None
        self.service.bind("163", account.address, "updated")
        self.assertEqual(self.vault.get(account.address), "updated")
        self.assertEqual(self.service.accounts(), [account])

    def test_partial_vault_failure_rolls_back_account_or_restores_credentials(self):
        self.vault.fail_next_write = True
        with self.assertRaises(RuntimeError):
            self.service.bind("qq", "me@qq.com", "bad")
        self.assertEqual(self.service.accounts(), [])
        self.assertEqual(self.vault.values, {})
        account = self.service.bind("qq", "me@qq.com", "old")
        self.vault.fail_next_write = True
        with self.assertRaises(RuntimeError):
            self.service.bind("qq", account.address, "bad")
        self.assertEqual(self.service.accounts(), [account])
        self.assertEqual(self.vault.get(account.address), "old")

    def test_database_commit_failure_restores_previous_vault_state(self):
        account = self.service.bind("qq", "me@qq.com", "previous")
        db = Mock(wraps=sqlite3.connect(self.db_path))
        db.commit.side_effect = sqlite3.OperationalError("disk full")
        with patch.object(self.service, "_connect", return_value=db):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.bind("qq", account.address, "replacement")
        self.assertEqual(self.vault.get(account.address), "previous")
        self.assertEqual(self.service.accounts(), [account])

    def test_reads_are_scoped_to_bound_account_and_credentials(self):
        qq = self.service.bind("qq", "me@qq.com", "qq-only")
        other = self.service.bind("163", "me@163.com", "163-only")
        item = MailItem(
            account=qq.address, validity="1", uid=8, subject="测试", sender="a",
            recipient=qq.address, date="", timestamp=0.0,
        )
        page = MailPage(items=[item], before_uid=None)
        self.client.list_messages.return_value = page
        self.client.read_body.return_value = "正文"
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(self.service.list_messages, qq, 10).result(), page)
            self.assertEqual(pool.submit(self.service.read_body, qq, item).result(), "正文")
        self.client.list_messages.assert_called_once_with(qq, "qq-only", 10)
        self.client.read_body.assert_called_once_with(qq, "qq-only", item)
        with self.assertRaises(ValueError):
            self.service.read_body(other, item)
        with self.assertRaises(ValueError):
            self.service.list_messages(Account(address=qq.address, provider="163"))
        self.vault.delete(qq.address)
        with self.assertRaises(ValueError):
            self.service.list_messages(qq)
        self.assertEqual(self.client.read_body.call_count, 1)
        self.assertEqual(self.client.list_messages.call_count, 1)
