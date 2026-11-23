import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from maildesk.models import ArrivalBatch, MailItem
from maildesk.service import MailService
from tests.mail_fixtures import MemoryVault


def message(account, uid, subject, preview="", flagged=False, validity="1"):
    return MailItem(account.address, validity, uid, subject, "发件人", account.address,
                    "2026-11-24", float(uid), preview=preview, unread=True, flagged=flagged)


class PendingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts.sqlite"
        self.client = Mock()
        self.vault = MemoryVault()
        self.service = MailService(self.path, self.client, self.vault)
        self.qq = self.service.bind("qq", "me@qq.com", "demo")
        self.netease = self.service.bind("163", "me@163.com", "demo")

    def scan(self, account, uid, items=(), validity="1"):
        self.client.scan_arrivals.return_value = ArrivalBatch(validity, uid, list(items))
        return self.service.poll_pending(account)

    def test_first_scan_rules_and_private_preview(self):
        self.assertEqual(self.scan(self.qq, 92), [])
        self.client.scan_arrivals.assert_called_with(self.qq, "demo", None, 0)
        items = [
            message(self.qq, 93, "周三开会", "请周二前确认是否参加。这是仅用于判断的预览内容"),
            message(self.qq, 94, "Action required: sign your invitation"),
            message(self.qq, 95, "读书会书单", flagged=True),
            message(self.qq, 96, "登录验证码", "请确认您的账户"),
            message(self.qq, 97, "状态更新", "操作已经完成，请勿回复。"),
            message(self.qq, 98, "本周会刊", "你好，随信附上本周的工作记录。"),
            message(self.qq, 99, "No action required", "For your records."),
        ]
        self.assertEqual(self.scan(self.qq, 99, items), items[:3])
        self.client.scan_arrivals.assert_called_with(self.qq, "demo", "1", 92)
        self.assertEqual([item.uid for item in self.service.list_pending()], [95, 94, 93])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("仅用于判断的预览内容", "\n".join(db.iterdump()))

    def test_completion_and_cursor_survive_restart_and_scope_identical_uids(self):
        for account in (self.qq, self.netease):
            self.scan(account, 0)
            self.scan(account, 1, [message(account, 1, "请审核")])
        pending = {item.account: item for item in self.service.list_pending()}
        netease = pending[self.netease.address]
        self.service.complete_pending(pending[self.qq.address])
        self.service = MailService(self.path, self.client, self.vault)
        self.assertEqual(self.scan(self.qq, 1), [])
        self.client.scan_arrivals.assert_called_with(self.qq, "demo", "1", 1)
        self.assertEqual(self.service.list_pending(), [netease])
        self.scan(self.netease, 0, validity="2")
        self.assertEqual(self.service.list_pending(), [])
        replacement = message(self.netease, 1, "请回复", validity="2")
        self.scan(self.netease, 1, [replacement], validity="2")
        self.service.complete_pending(netease)
        self.assertEqual([item.validity for item in self.service.list_pending()], ["2"])

    def test_concurrent_pollers_notify_only_once(self):
        self.scan(self.qq, 0)
        other_process = MailService(self.path, self.client, self.vault)
        barrier = threading.Barrier(2)
        item = message(self.qq, 1, "审批申请", "Could you review the draft?")

        def scan_together(*_):
            barrier.wait(timeout=5)
            return ArrivalBatch("1", 1, [item])

        self.client.scan_arrivals.side_effect = scan_together
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(instance.poll_pending, self.qq) for instance in (self.service, other_process)]
            self.assertCountEqual([future.result() for future in results], [[item], []])
        self.assertEqual(len(self.service.list_pending()), 1)

    def test_failed_partial_batch_does_not_advance_sync(self):
        self.scan(self.qq, 10)
        good = message(self.qq, 11, "请反馈")
        wrong_account = message(self.netease, 12, "请回复")
        with self.assertRaises(ValueError):
            self.scan(self.qq, 12, [good, wrong_account])
        self.assertEqual(self.service.list_pending(), [])
        self.assertEqual(self.scan(self.qq, 12, [good]), [good])
        self.client.scan_arrivals.assert_called_with(self.qq, "demo", "1", 10)

    def test_mark_read_only_changes_confirmed_flags_and_keeps_both_tasks(self):
        for account in (self.qq, self.netease):
            self.scan(account, 0)
            self.scan(account, 1, [message(account, 1, "请审核", flagged=True)])
        qq_item = next(item for item in self.service.list_pending() if item.account == self.qq.address)
        with self.assertRaises(ValueError):
            self.service.mark_read(self.netease, qq_item)
        self.client.mark_read.assert_not_called()
        self.client.mark_read.side_effect = ConnectionError
        with self.assertRaises(ConnectionError):
            self.service.mark_read(self.qq, qq_item)
        self.assertTrue(all(item.unread for item in self.service.list_pending()))
        self.client.mark_read.side_effect = None
        self.service.mark_read(self.qq, qq_item)
        self.client.mark_read.assert_called_with(self.qq, "demo", qq_item)
        reopened = MailService(self.path, self.client, self.vault)
        pending = {item.account: item for item in reopened.list_pending()}
        self.assertEqual(pending[self.qq.address], replace(qq_item, unread=False))
        self.assertTrue(pending[self.netease.address].unread)

    def test_read_while_scan_is_in_flight_cannot_restore_unread_in_queue(self):
        self.scan(self.qq, 0)
        new_mail = message(self.qq, 1, "请确认会议")

        def concurrent_read(*_):
            self.service.mark_read(self.qq, new_mail)
            return ArrivalBatch("1", 1, [new_mail])

        self.client.scan_arrivals.side_effect = concurrent_read
        expected = [replace(new_mail, unread=False)]
        self.assertEqual(self.service.poll_pending(self.qq), expected)
        self.assertEqual(self.service.list_pending(), expected)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM reads_ahead_of_cursor").fetchall(), [])
