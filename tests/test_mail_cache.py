import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from maildesk.models import MailBody, MailPage, ReplyEnvelope
from maildesk.service import MailService
from tests.mail_fixtures import ACCOUNTS, FixtureClient, MemoryVault, PNG


class MailCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts.sqlite3"
        self.client = FixtureClient()
        self.service = MailService(self.path, self.client, MemoryVault())
        self.account, self.other = ACCOUNTS
        for account in ACCOUNTS:
            self.service.bind(account.provider, account.address, "test")
        self.item = self.service.list_messages(self.account).items[0]
        self.key = self.item.account, self.item.validity, self.item.uid

    def test_warm_start_reads_list_body_images_and_reply_headers_offline(self):
        body = MailBody("来信内容", "<p>来信</p>", {"cid:logo": PNG}, "https://example.org/",
                        ReplyEnvelope("sender@example.org", "Re: 来信", "<a>", ("<a>",)))
        self.client.read_content = Mock(return_value=body)
        expected = self.service.read_content(self.account, self.item)
        url = "https://example.org/mail.png"
        fetch = Mock(return_value={url: PNG})
        self.service.read_images(self.item, (url,), fetch, cancelled=lambda: False)
        self.service.mark_read(self.account, self.item)
        offline = MailService(self.path, Mock(), MemoryVault())
        snap = offline.cached_first_page(self.account)
        self.assertEqual(snap.items[0], replace(self.item, unread=False))
        self.assertIsNone(snap.next_cursor)
        self.assertEqual(offline.read_content(self.account, snap.items[0]), expected)
        self.assertEqual(offline.read_images(self.item, (url,), fetch, cancelled=lambda: False), {url: PNG})
        fetch.assert_called_once()
        self.assertEqual(expected.reply.source, self.key)
        with self.assertRaises(ValueError):
            offline.read_content(self.other, self.item)
        self.assertIsNone(offline.cached_first_page(self.other))
        self.assertEqual(offline._client.mock_calls, [])

    def test_server_replacement_and_read_confirmed_during_a_fetch(self):
        early = time.time_ns()
        self.service.read_content(self.account, self.item)
        self.service.mark_read(self.account, self.item)
        self.service._cache.save_page(self.account.address, MailPage([self.item], None), early)
        self.assertFalse(self.service.cached_first_page(self.account).items[0].unread)
        fresh = replace(self.item, validity="2", subject="新邮件")
        self.client.list_messages = Mock(return_value=MailPage([fresh], None))
        self.service.list_messages(self.account)
        self.assertIsNone(self.service._cache.body(self.key))
        self.service._cache.save_page(self.account.address, MailPage([self.item], None), early)
        self.assertEqual(self.service.cached_first_page(self.account).items, [fresh])
        self.client.list_messages.return_value = MailPage([], None)
        self.service.list_messages(self.account)
        self.assertEqual(self.service.cached_first_page(self.account).items, [])

    def test_overlapping_body_requests_share_one_download_and_failed_reads_retry(self):
        entered, release = Event(), Event()
        def read(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return MailBody("一次获取")
        self.client.read_content = Mock(side_effect=read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.read_content, self.account, self.item)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.service.read_content, self.account, self.item)
            release.set()
            self.assertEqual(first.result(), second.result())
        self.client.read_content.assert_called_once()
        other_item = replace(self.item, uid=self.item.uid + 1)
        self.client.read_content.side_effect = [OSError("offline"), MailBody("恢复后")]
        with self.assertRaises(OSError):
            self.service.read_content(self.account, other_item)
        self.assertEqual(self.service.read_content(self.account, other_item).text, "恢复后")

    def test_asset_budget_and_image_retry(self):
        cache = self.service._cache
        with patch("maildesk.mail_cache._CACHE_BYTES", 6), patch("maildesk.mail_cache._ENTRY_BYTES", 5):
            cache.save_images(self.key, {"a": b"12", "b": b"34"})
            self.assertEqual(cache.images(self.key, ["a"]), {"a": b"12"})
            cache.save_images(self.key, {"c": b"567", "too-big": b"x" * 6})
            self.assertEqual(cache.images(self.key, ["a", "b", "c", "too-big"]), {"a": b"12", "c": b"567"})
        fetch = Mock(return_value={"a": b"fresh"})
        self.assertEqual(self.service.read_images(self.item, ("a",), fetch, retry=True, cancelled=lambda: False),
                         {"a": b"fresh"})

    def test_cache_failure_does_not_block_live_mail(self):
        with patch.object(self.service._cache, "_connect", side_effect=sqlite3.OperationalError("disk full")):
            self.assertIsNone(self.service.cached_first_page(self.account))
            self.assertEqual(self.service.list_messages(self.account).items[0], self.item)
            self.assertTrue(self.service.read_content(self.account, self.item).text)
