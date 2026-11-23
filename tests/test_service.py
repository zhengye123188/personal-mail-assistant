import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from maildesk.models import Account, MailItem, MailPage
from maildesk.providers import provider_for_address
from maildesk.service import MailService


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
