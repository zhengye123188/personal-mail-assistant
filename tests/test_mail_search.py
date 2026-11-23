import imaplib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from maildesk.demo import DemoVault
from maildesk.mail_client import MailClient, MailClientError
from maildesk.models import MailQuery
from maildesk.service import MailService
from tests.mail_fixtures import ACCOUNTS, FixtureClient


class ImapSearchTests(unittest.TestCase):
    def setUp(self):
        provider = patch("maildesk.mail_client.imaplib.IMAP4_SSL")
        self.imap = provider.start().return_value
        self.addCleanup(provider.stop)
        self.imap.capabilities = (b"PREVIEW",)
        self.imap.literal = None
        self.imap.login.return_value = self.imap.xatom.return_value = ("OK", [])
        self.imap.select.return_value = ("OK", [b"4"])
        self.imap.response.return_value = ("UIDVALIDITY", [b"77"])
        self.imap.uid.side_effect = self.command
        self.searches = []
        self.headers = []
        self.dates = {1: 23, 2: 10, 3: 8, 4: 11}
        self.responses = []
        self.search = MailClient().search_mail

    def command(self, action, *args):
        if action == "SEARCH":
            self.searches.append((args, self.imap.literal))
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return ("NO", []) if result is None else ("OK", [result])
        if action == "SORT":
            self.assertEqual(args, ("(REVERSE ARRIVAL)", "US-ASCII", "ALL"))
            return "OK", [b"1 4 2 3"]
        self.assertEqual(action, "FETCH")
        uids, fields = args
        rows = []
        for entry in uids.split(","):
            uid = int(entry)
            received = f'{self.dates[uid]:02}-Nov-2026 10:00:00 +0800'
            if fields == "(UID INTERNALDATE)":
                rows.append(f'{uid} (UID {uid} INTERNALDATE "{received}")'.encode())
            elif fields == "(UID PREVIEW)":
                snippet = "项目" if uid in (1, 2) else "普通邮件"
                rows.append(f'{uid} (UID {uid} PREVIEW "{snippet}")'.encode())
            else:
                self.assertIn("BODY.PEEK[HEADER.FIELDS", fields)
                self.headers.append(uid)
                raw = f"From: Person <a{uid}@example.org>\r\nSubject: mail {uid}\r\n\r\n".encode()
                head = f'{uid} (UID {uid} FLAGS (\\Flagged) INTERNALDATE "{received}" BODY[HEADER] {{{len(raw)}}}'
                rows.extend([(head.encode(), raw), b")"])
        return "OK", rows

    def test_intersection_of_two_utf8_literals_uses_dates_and_flags(self):
        query = MailQuery(sender="同事", text="项目", since=date(2026, 11, 7),
                          before=date(2026, 11, 24), unread_only=True, flagged_only=True)
        self.responses = [b"1 2", b"1 3"]
        result = self.search(ACCOUNTS[1], "demo", query)
        prefix = (None, "CHARSET", "UTF-8", "SINCE", "07-Nov-2026", "BEFORE", "24-Nov-2026", "UNSEEN", "FLAGGED")
        self.assertEqual(self.searches, [(prefix + ("FROM",), "同事".encode()),
                                        (prefix + ("TEXT",), "项目".encode())])
        self.assertEqual([item.uid for item in result.items], [1])
        self.assertFalse(result.truncated)
        self.imap.select.assert_called_once_with("INBOX", readonly=True)

    def test_quoted_ascii_and_date_order_independent_of_uid(self):
        self.responses = [b"1 2 3 4"]
        result = self.search(ACCOUNTS[0], "demo", MailQuery(text='a"b\\c'), limit=2)
        self.assertEqual(self.searches, [((None, "ALL", "TEXT", '"a\\"b\\\\c"'), None)])
        self.assertEqual([item.uid for item in result.items], [1, 4])
        self.assertEqual(self.headers, [1, 4])
        self.assertTrue(result.truncated)
        self.assertFalse(result.notice)
        with self.assertRaises(ValueError):
            MailQuery(sender="a\nb")

    @patch("maildesk.mail_client._SEARCH_PREVIEW_CAP", 2)
    def test_unsupported_keyword_bounds_preview_search_and_preserves_sender_filter(self):
        self.responses = [b"1 2 4", imaplib.IMAP4.error("BAD")]
        result = self.search(ACCOUNTS[1], "demo", MailQuery(text="项目", sender="example.org"))
        self.assertEqual(self.headers, [1, 4])
        self.assertEqual([item.uid for item in result.items], [1])
        self.assertTrue(result.truncated)
        self.assertIn("2 封", result.notice)
        self.assertIn("预览", result.notice)
        self.assertIsNone(self.imap.literal)
        self.responses = [None, b"2 3"]
        empty = self.search(ACCOUNTS[1], "demo", MailQuery(text="不存在"))
        self.assertEqual(empty.items, [])
        self.assertTrue(empty.truncated)
        self.assertTrue(empty.notice)

    @patch("maildesk.mail_client._SEARCH_DATE_CAP", 2)
    def test_large_search_uses_native_arrival_sort_or_reports_limited_date_order(self):
        self.responses = [b"1 2 3 4", b"1 2 3 4"]
        query = MailQuery()
        partial = self.search(ACCOUNTS[0], "demo", query, limit=1)
        self.assertIn("2 封", partial.notice)
        self.assertEqual([item.uid for item in partial.items], [4])
        self.imap.capabilities += (b"SORT",)
        ordered = self.search(ACCOUNTS[0], "demo", query, limit=1)
        self.assertFalse(ordered.notice)
        self.assertEqual([item.uid for item in ordered.items], [1])

    def test_server_failure_is_distinct_from_successful_empty_result(self):
        self.responses = [None, b""]
        with self.assertRaises(MailClientError):
            self.search(ACCOUNTS[0], "demo", MailQuery())
        self.assertFalse(self.search(ACCOUNTS[0], "demo", MailQuery()).truncated)

    def test_sender_fallback_can_cover_every_candidate_using_only_headers(self):
        self.responses = [None, b"1 3"]
        result = self.search(ACCOUNTS[0], "demo", MailQuery(sender="a3@example.org"))
        self.assertEqual([item.uid for item in result.items], [3])
        self.assertIn("发件人", result.notice)
        self.assertFalse(result.truncated)


class DemoSearchTests(unittest.TestCase):
    def test_account_scoped_read_only_search_flags_dates_and_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FixtureClient()
            service = MailService(Path(directory) / "demo.sqlite", client, DemoVault())
            qq, netease = ACCOUNTS
            query = MailQuery(text="二楼阅览室", sender="READER@example.org", unread_only=True)
            with self.assertRaisesRegex(ValueError, "尚未绑定"):
                service.search_mail(qq, query)
            for account in ACCOUNTS:
                service.bind(account.provider, account.address, "demo")
            items = service.search_mail(qq, query).items
            self.assertEqual([m.uid for m in items], [3])
            self.assertTrue(service.search_mail(qq, query).items[0].unread)
            self.assertFalse(service.search_mail(netease, query).items)
            flagged = service.search_mail(netease, MailQuery(flagged_only=True)).items
            self.assertEqual([m.uid for m in flagged], [3])
            received = date.fromtimestamp(flagged[0].timestamp)
            self.assertFalse(service.search_mail(netease, MailQuery(flagged_only=True, before=received)).items)
            self.assertTrue(service.search_mail(netease, MailQuery(flagged_only=True, since=received,
                                                                   before=received + timedelta(days=1))).items)
            service.mark_read(qq, items[0])
            self.assertFalse(service.search_mail(qq, query).items)
            self.assertTrue(service.search_mail(qq, MailQuery(), limit=1).truncated)
            with self.assertRaises(ValueError):
                service.search_mail(qq, query, limit=41)
