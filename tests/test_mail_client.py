import imaplib
import smtplib
import ssl
import unittest
from dataclasses import replace
from email.message import EmailMessage
from unittest.mock import call, patch

from maildesk.mail_client import MailClient, MailClientError
from maildesk.models import Account, MailItem


ACCOUNT = Account("student@163.com", "163")
ITEM = MailItem(ACCOUNT.address, "77", 42, "", "", "", "", 0)


class MailClientTests(unittest.TestCase):
    def setUp(self):
        self.client = MailClient()
        imap_patch = patch("maildesk.mail_client.imaplib.IMAP4_SSL")
        self.factory = imap_patch.start()
        self.addCleanup(imap_patch.stop)
        self.imap = self.factory.return_value
        self.imap.capabilities = ("IMAP4REV1",)
        self.imap.uid.return_value = ("OK", [])
        self.imap.login.return_value = ("OK", [b"authenticated"])
        self.imap.xatom.return_value = ("OK", [b"identified"])
        self.imap.select.return_value = ("OK", [b"3"])
        self.imap.response.return_value = ("UIDVALIDITY", [b"77"])

    @patch("maildesk.mail_client.smtplib.SMTP_SSL")
    def test_verify_implicit_tls_providers_authenticates_and_selects_inbox(self, smtp):
        cases = [
            ("qq", "qq.com", "qq.com"), ("163", "163.com", "163.com"),
            ("126", "126.com", "126.com"), ("yeah", "yeah.net", "yeah.net"),
            ("gmail", "gmail.com", "gmail.com"), ("yahoo", "yahoo.com", "mail.yahoo.com"),
        ]
        for provider, domain, host_domain in cases:
            with self.subTest(provider=provider):
                account = Account(f"student@{domain}", provider)
                self.client.verify(account, "dummy")
                calls = [call.login(account.address, "dummy")]
                if provider in {"163", "126", "yeah"}:
                    calls.append(call.xatom("ID", '("name" "MailDesk" "version" "1.0")'))
                calls.append(call.select("INBOX", readonly=True))
                self.imap.assert_has_calls(calls)
                self.assertEqual(self.factory.call_args.args, (f"imap.{host_domain}", 993))
                self.assertEqual(smtp.call_args.args, (f"smtp.{host_domain}", 465))
                for args, name in ((self.factory.call_args, "ssl_context"), (smtp.call_args, "context")):
                    self.assertTrue(args.kwargs[name].check_hostname)
                    self.assertEqual(args.kwargs[name].verify_mode, ssl.CERT_REQUIRED)
                    self.assertGreater(args.kwargs["timeout"], 0)
                self.assertEqual(smtp.return_value.method_calls, [
                    call.login(account.address, "dummy"), call.quit(), call.close(),
                ])
                self.imap.reset_mock()
                smtp.reset_mock()

    @patch("maildesk.mail_client.smtplib.SMTP")
    def test_icloud_tries_full_imap_address_on_auth_rejection_and_upgrades_smtp(self, smtp):
        account = Account("student@icloud.com", "icloud")
        self.imap.login.side_effect = [imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] denied"), ("OK", [])]
        self.client.verify(account, "dummy")
        self.assertEqual(self.factory.call_args.args, ("imap.mail.me.com", 993))
        self.assertEqual(self.imap.login.call_args_list, [call("student", "dummy"), call(account.address, "dummy")])
        self.imap.xatom.assert_not_called()
        self.assertEqual(smtp.call_args.args, ("smtp.mail.me.com", 587))
        tls = self.factory.call_args.kwargs["ssl_context"]
        self.assertTrue(tls.check_hostname)
        self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(smtp.return_value.method_calls, [
            call.starttls(context=tls), call.login(account.address, "dummy"), call.quit(), call.close(),
        ])
        self.imap.login.reset_mock(side_effect=True)
        self.client.verify(account, "dummy")
        self.imap.login.assert_called_once_with(account.address, "dummy")

    @patch("maildesk.mail_client.smtplib.SMTP")
    def test_icloud_does_not_retry_imap_on_service_errors(self, smtp):
        account = Account("student@icloud.com", "icloud")
        for error in (imaplib.IMAP4.abort("disconnected"), imaplib.IMAP4.error(b"[UNAVAILABLE] service down")):
            with self.subTest(error=error):
                self.imap.login.side_effect = error
                with self.assertRaises(MailClientError):
                    self.client.verify(account, "dummy")
                self.imap.login.assert_called_once_with("student", "dummy")
                self.imap.reset_mock()
        smtp.assert_not_called()

    @patch("maildesk.mail_client.smtplib.SMTP")
    def test_icloud_smtp_never_sends_credentials_if_starttls_fails(self, smtp):
        account = Account("student@icloud.com", "icloud")
        smtp.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS unavailable")
        with self.assertRaisesRegex(MailClientError, "SMTP"):
            self.client.verify(account, "dummy")
        self.imap.login.assert_called_once_with("student", "dummy")
        smtp.return_value.login.assert_not_called()
        smtp.return_value.close.assert_called_once_with()

    def _mock_dated_inbox(self, dates):
        """Keep separate logs for the lightweight date index and visible message content."""
        headers, previews, metadata = [], [], []
        self.imap.select.side_effect = lambda *a, **kw: ("OK", [str(len(dates)).encode()])
        self.imap.capabilities += (b"PREVIEW",)

        def fetch_uids(uids, fields):
            live = sorted(dates)
            lines = []
            for uid in sorted(set(uids) & dates.keys()):
                seq = live.index(uid) + 1
                received = dates[uid]
                if fields == "(UID INTERNALDATE)":
                    metadata.append(uid)
                    lines.append(f'{seq} (INTERNALDATE "{received}" UID {uid})'.encode())
                elif fields == "(UID PREVIEW)":
                    previews.append(uid)
                    lines.append(f'{seq} (UID {uid} PREVIEW "sample")'.encode())
                else:
                    self.assertIn("BODY.PEEK[HEADER.FIELDS", fields)
                    headers.append(uid)
                    raw = b"Subject: =?gb2312?B?1tDOxA==?=\r\nFrom: test@example.com\r\n\r\n"
                    prefix = f'{seq} (INTERNALDATE "{received}" BODY[HEADER.FIELDS (SUBJECT)] {{{len(raw)}}}'
                    lines.extend([(prefix.encode(), raw), f" UID {uid})".encode()])
            return "OK", lines

        def uid_command(command, *args):
            if command == "SEARCH":
                if args[-1] == "ALL":
                    found = dates
                else:
                    self.assertEqual(args[:2], (None, "UID"))
                    found = {int(uid) for uid in args[2].split(",")} & dates.keys()
                return "OK", [" ".join(str(uid) for uid in sorted(found)).encode()]
            self.assertEqual(command, "FETCH")
            uid_set, fields = args
            return fetch_uids([int(uid) for uid in uid_set.split(",")], fields)

        def sequence_fetch(sequence, fields):
            if fields == "(UID)":
                uid = sorted(dates)[int(sequence) - 1]
                return "OK", [f"{sequence} (UID {uid})".encode()]
            low, high = map(int, sequence.split(":"))
            return fetch_uids(sorted(dates)[low - 1:high], fields)

        self.imap.fetch.side_effect = sequence_fetch
        self.imap.uid.side_effect = uid_command
        return headers, previews, metadata

    def test_qq_pages_by_global_received_date_despite_later_uids_belonging_to_old_mail(self):
        account = Account("student@qq.com", "qq")
        dates = dict.fromkeys(range(1, 91), "03-Jan-2020 10:00:00 +0800")
        dates[1] = "03-Jan-2026 10:00:00 +0800"
        dates[18] = "03-Jan-2026 02:00:00 +0000"  # Same instant; larger UID wins the tie.
        expected = [18, 1] + [uid for uid in reversed(dates) if uid not in (18, 1)]
        headers, previews, metadata = self._mock_dated_inbox(dates)

        first = self.client.list_messages(account, "dummy")
        self.assertEqual([item.uid for item in first.items], expected[:40])
        self.assertEqual(first.items[0].timestamp, first.items[1].timestamp)
        self.assertEqual(first.items[0].subject, "中文")
        self.assertCountEqual(headers, expected[:40])
        self.assertCountEqual(previews, expected[:40])
        self.assertCountEqual(metadata, expected)

        del dates[expected[39]]  # Previously displayed page boundary.
        del dates[expected[40]]  # Expunged before its page was fetched.
        dates[100] = "04-Jan-2026 10:00:00 +0800"
        dates[101] = "01-Jan-2018 10:00:00 +0800"  # Imported history with a new UID.
        second = self.client.list_messages(account, "dummy", first.next_cursor)
        third = self.client.list_messages(account, "dummy", second.next_cursor)
        self.assertEqual(len(second.items), 40)
        self.assertEqual([m.uid for m in second.items + third.items], expected[41:])
        self.assertIsNone(third.next_cursor)
        self.assertCountEqual(metadata, expected)
        self.assertCountEqual(headers, expected[:40] + expected[41:])
        self.assertCountEqual(previews, expected[:40] + expected[41:])
        self.assertTrue(all(c == call("INBOX", readonly=True) for c in self.imap.select.call_args_list))
        self.imap.xatom.assert_not_called()

        metadata.clear()
        refreshed = self.client.list_messages(account, "dummy")
        self.assertEqual(refreshed.items[0].uid, 100)
        self.assertEqual(refreshed.next_cursor.ordered_uids[-1], 101)
        self.assertCountEqual(metadata, [100, 101])
        metadata.clear()
        self.client.list_messages(account, "dummy")
        self.assertEqual(metadata, [])

    def test_native_sort_handles_ties_across_pages_without_fetching_all_dates(self):
        dates = dict.fromkeys(range(1, 91), "03-Jan-2026 10:00:00 +0800")
        dates[89] = "03-Jan-2020 10:00:00 +0800"
        headers, previews, metadata = self._mock_dated_inbox(dates)
        self.imap.capabilities = ("IMAP4REV1", "SORT=DISPLAY", "PREVIEW")
        command = self.imap.uid.side_effect

        def sort_or_fetch(action, *args):
            if action == "SORT":
                self.assertEqual(args, ("(ARRIVAL)", "US-ASCII", "ALL"))
                order = [89, *range(1, 89), 90]
                return "OK", [" ".join(map(str, order)).encode()]
            self.assertNotEqual(action, "SEARCH")
            return command(action, *args)

        self.imap.uid.side_effect = sort_or_fetch
        first = self.client.list_messages(ACCOUNT, "dummy")
        self.assertEqual(first.next_cursor.ordered_uids, (90, *range(88, 0, -1), 89))
        self.assertCountEqual(headers, first.next_cursor.ordered_uids[:40])
        self.assertCountEqual(previews, headers)
        self.assertFalse(metadata)

    def test_unavailable_or_partial_sort_falls_back_to_live_mailbox_dates(self):
        dates = dict.fromkeys(range(1, 48), "03-Jan-2026 10:00:00 +0800")
        _, _, metadata = self._mock_dated_inbox(dates)
        self.imap.capabilities += (b"SORT",)
        command = self.imap.uid.side_effect
        for reply in (("NO", []), ("OK", [b"5"]), ("OK", [b"invalid"]), imaplib.IMAP4.error("BAD")):
            with self.subTest(reply=reply):
                def partial_sort(action, *args):
                    if action == "SORT":
                        if isinstance(reply, Exception):
                            raise reply
                        return reply
                    return command(action, *args)

                self.imap.uid.side_effect = partial_sort
                result = self.client.list_messages(ACCOUNT, "dummy")
                self.assertEqual(result.next_cursor.ordered_uids, tuple(range(47, 0, -1)))
        self.assertCountEqual(metadata, dates)

    def test_date_index_isolated_by_account_and_uidvalidity(self):
        dates = dict.fromkeys(range(1, 55), "03-Jan-2026 10:00:00 +0800")
        _, _, metadata = self._mock_dated_inbox(dates)
        self.client.list_messages(ACCOUNT, "dummy")
        dates[1] = "04-Jan-2026 10:00:00 +0800"
        other = Account("other@163.com", "163")
        for account in (other, ACCOUNT):
            with self.subTest(account=account):
                if account == ACCOUNT:
                    self.imap.response.return_value = ("UIDVALIDITY", [b"99"])
                metadata.clear()
                self.assertEqual(self.client.list_messages(account, "dummy").items[0].uid, 1)
                self.assertCountEqual(metadata, dates)

    @patch("maildesk.mail_client._DATE_CACHE_LIMIT", 44)
    def test_bounded_date_index_refetches_evicted_dates_without_losing_old_messages(self):
        dates = dict.fromkeys(range(1, 51), "03-Jan-2020 10:00:00 +0800")
        dates[1] = "04-Jan-2026 10:00:00 +0800"
        _, _, metadata = self._mock_dated_inbox(dates)
        self.client.list_messages(ACCOUNT, "dummy")
        metadata.clear()
        refreshed = self.client.list_messages(ACCOUNT, "dummy")
        self.assertCountEqual(metadata, range(1, 7))
        self.assertEqual(refreshed.items[0].uid, 1)

    def test_date_cursor_detects_replaced_mailbox_before_loading_reused_uids(self):
        dates = dict.fromkeys(range(1, 43), "03-Jan-2020 10:00:00 +0800")
        headers, previews, metadata = self._mock_dated_inbox(dates)
        cursor = self.client.list_messages(ACCOUNT, "dummy").next_cursor
        headers.clear()
        previews.clear()
        metadata.clear()
        self.imap.response.return_value = ("UIDVALIDITY", [b"78"])
        with self.assertRaisesRegex(MailClientError, "刷新"):
            self.client.list_messages(ACCOUNT, "dummy", cursor)
        self.assertFalse(headers or previews or metadata)

    def test_explicit_uid_page_after_disappeared_anchor_remains_supported(self):
        dates = dict.fromkeys((1, 2, 4, 5), "03-Jan-2022 10:00:00 +0800")
        self._mock_dated_inbox(dates)
        older = self.client.list_messages(ACCOUNT, "dummy", before_uid=3)
        self.assertEqual([item.uid for item in older.items], [2, 1])
        self.assertEqual(older.items[0].timestamp, 1641175200)
        self.assertIsNone(older.next_cursor)

    def test_empty_inbox(self):
        self.imap.select.return_value = ("OK", [b"0"])
        page = self.client.list_messages(ACCOUNT, "dummy")
        self.assertEqual(page.items, [])
        self.assertIsNone(page.before_uid)
        self.imap.fetch.assert_not_called()
        self.imap.uid.assert_not_called()

    def test_arrival_baseline_uidvalidity_reset_and_empty_mailboxes_never_fetch_history(self):
        self.imap.fetch.return_value = ("OK", [b"3 (UID 91)"])
        for previous in (None, "76"):
            batch = self.client.scan_arrivals(ACCOUNT, "dummy", previous, 200)
            self.assertEqual((batch.validity, batch.through_uid, batch.items), ("77", 91, []))
        # The previous highest UID may have been expunged within the same epoch.
        self.assertEqual(self.client.scan_arrivals(ACCOUNT, "dummy", "77", 200).through_uid, 200)
        self.imap.select.return_value = ("OK", [b"0"])
        self.assertEqual(self.client.scan_arrivals(ACCOUNT, "dummy", None, 200).through_uid, 0)
        self.assertEqual(self.client.scan_arrivals(ACCOUNT, "dummy", "77", 200).through_uid, 200)
        self.imap.uid.assert_not_called()

    def test_arrival_pages_cover_sparse_uid_backlog_in_ascending_order(self):
        uids = [10_000 * i for i in range(1, 94)]
        self.imap.select.return_value = ("OK", [str(len(uids)).encode()])
        self.imap.fetch.side_effect = lambda seq, _: ("OK", [f"{seq} (UID {uids[int(seq) - 1]})".encode()])
        self.imap.capabilities += (b"PREVIEW",)

        def uid_command(command, *args):
            if command == "SEARCH":
                low, high = map(int, args[2].split(":"))
                found = [str(uid) for uid in reversed(uids) if low <= uid <= high]
                self.assertLessEqual(len(found), 40)
                return "OK", [" ".join(found).encode()]
            self.assertEqual(command, "FETCH")
            ids, fields = args
            if fields == "(UID PREVIEW)":
                return "OK", [f'1 (UID {uid} PREVIEW "please reply")'.encode() for uid in ids.split(",")]
            self.assertIn("BODY.PEEK[HEADER.FIELDS", fields)
            return "OK", [(f"1 (UID {uid} FLAGS () BODY[HEADER.FIELDS] {{18}}".encode(),
                           b"Subject: hi\r\n\r\n") for uid in ids.split(",")]

        self.imap.uid.side_effect = uid_command
        seen = []
        after_uid = 10_000
        for size in (40, 40, 12, 0):
            batch = self.client.scan_arrivals(ACCOUNT, "dummy", "77", after_uid)
            self.assertEqual(len(batch.items), size)
            self.assertTrue(all(item.preview == "please reply" and item.unread for item in batch.items))
            seen.extend(item.uid for item in batch.items)
            after_uid = batch.through_uid
        self.assertEqual(seen, uids[1:])

    def test_arrivals_do_not_advance_when_headers_or_previews_are_missing(self):
        self.imap.fetch.return_value = ("OK", [b"3 (UID 11)"])
        one_header = (b"1 (UID 10 BODY[HEADER.FIELDS] {14}", b"Subject: a\r\n\r\n")
        another_header = (b"2 (UID 11 BODY[HEADER.FIELDS] {14}", b"Subject: b\r\n\r\n")
        responses = [
            [("OK", [one_header])],
            [("OK", [one_header, another_header]), ("OK", [None])],
            [("OK", [one_header, another_header]), OSError("disconnected")],
        ]
        for replies in responses:
            with self.subTest(replies=replies):
                self.imap.uid.side_effect = [("OK", [b"10 11"]), *replies]
                with self.assertRaises(MailClientError):
                    self.client.scan_arrivals(ACCOUNT, "dummy", "77", 9)

    def _mock_list_headers(self):
        raw = b"Subject: subject\r\n\r\n"
        self.imap.fetch.return_value = ("OK", [
            (b"1 (BODY[HEADER.FIELDS (SUBJECT)] {20}", raw), b" UID 8 FLAGS (\\Seen \\Flagged))",
            (b"2 (UID 9 FLAGS () BODY[HEADER.FIELDS (SUBJECT)] {20}", raw), b")",
            (b"3 (UID 10 BODY[HEADER.FIELDS (SUBJECT)] {20}", raw), b")",
        ])

    def test_reads_flags_and_quoted_or_literal_server_previews_by_uid(self):
        self._mock_list_headers()
        self.imap.capabilities += (b"PREVIEW",)
        self.imap.uid.return_value = ("OK", [
            (b"2 (PREVIEW {6}", "提醒".encode()), b" UID 9)",
            b'1 (PREVIEW "Use UID 100 \\"tomorrow\\"" UID 8)',
            b'3 (UID 10 PREVIEW "")',
        ])
        page = self.client.list_messages(ACCOUNT, "dummy")
        by_uid = {m.uid: (m.unread, m.flagged, m.preview) for m in page.items}
        self.assertEqual(by_uid[8], (False, True, 'Use UID 100 "tomorrow"'))
        self.assertEqual(by_uid[9], (True, False, "提醒"))
        self.assertEqual(by_uid[10], (None, None, ""))
        self.assertIn("FLAGS", self.imap.fetch.call_args.args[1])
        self.imap.uid.assert_called_once_with("FETCH", "8,9,10", "(UID PREVIEW)")

    def test_peek_batch_skips_attachments_decodes_partial_mime_and_ignores_incomplete_headers(self):
        self._mock_list_headers()
        mail = EmailMessage()
        mail.set_content("<style>hide</style><p>午餐在楼下</p>", subtype="html", charset="gb2312", cte="base64")
        # Extra orphaned base64 characters simulate a truncated FETCH payload.
        partial = mail.as_bytes() + b"X"
        attachment = b"Content-Disposition: attachment; filename=notes.txt\r\n\r\nattachment"
        self.imap.uid.return_value = ("OK", [
            (b"1 (UID 8 BODY[]<0> {123}", partial), b")",
            (b"2 (UID 9 BODY[]<0> {101}", attachment), b")",
            (b"3 (UID 10 BODY[]<0> {99}", b"Subject: long header, still incomplete"), b")",
        ])
        page = self.client.list_messages(ACCOUNT, "dummy")
        self.assertEqual({m.uid: m.preview for m in page.items}, {8: "午餐在楼下", 9: "", 10: ""})
        self.imap.uid.assert_called_once_with("FETCH", "8,9,10", "(UID BODY.PEEK[]<0.12288>)")

    def test_optional_preview_failure_does_not_hide_mail(self):
        self._mock_list_headers()
        self.imap.uid.side_effect = imaplib.IMAP4.error("unsupported extension")
        page = self.client.list_messages(ACCOUNT, "dummy")
        self.assertEqual([m.uid for m in page.items], [10, 9, 8])
        self.assertTrue(all(m.preview == "" for m in page.items))

    def test_stale_uidvalidity_or_other_account_never_fetches_body(self):
        with self.assertRaises(MailClientError):
            self.client.read_body(Account("other@163.com", "163"), "dummy", ITEM)
        self.factory.assert_not_called()
        self.imap.response.return_value = ("UIDVALIDITY", [b"78"])
        with self.assertRaisesRegex(MailClientError, "刷新"):
            self.client.read_body(ACCOUNT, "dummy", ITEM)
        self.imap.uid.assert_not_called()

    def test_mark_read_adds_only_seen_and_confirms_target_flags(self):
        responses = {"UIDVALIDITY": [b"77"], "PERMANENTFLAGS": [b"(\\Seen \\Flagged)"]}
        self.imap.response.side_effect = lambda name: (name, responses[name])
        for account in (ACCOUNT, Account("student@qq.com", "qq")):
            with self.subTest(account=account.provider):
                self.imap.reset_mock()
                self.imap.uid.side_effect = [
                    ("OK", [None]),
                    ("OK", [b"1 (UID 87 FLAGS ())", b"2 (FLAGS (\\Flagged \\sEen) UID 42)"]),
                ]
                self.client.mark_read(account, "dummy", replace(ITEM, account=account.address))
                self.imap.select.assert_called_once_with("INBOX", readonly=False)
                self.assertEqual(self.imap.uid.call_args_list, [
                    call("STORE", "42", "+FLAGS.SILENT", r"(\Seen)"),
                    call("FETCH", "42", "(UID FLAGS)"),
                ])
                self.imap.close.assert_not_called()
                self.imap.logout.assert_called_once()

    def test_mark_read_refuses_invalid_or_reused_uid_before_store(self):
        for item in (replace(ITEM, account="other@163.com"), replace(ITEM, uid=0),
                     replace(ITEM, uid="1:*"), replace(ITEM, uid=True), replace(ITEM, uid=2**32)):
            with self.subTest(item=item), self.assertRaises(MailClientError):
                self.client.mark_read(ACCOUNT, "dummy", item)
        self.factory.assert_not_called()
        self.imap.response.return_value = ("UIDVALIDITY", [b"78"])
        with self.assertRaisesRegex(MailClientError, "收件箱标识"):
            self.client.mark_read(ACCOUNT, "dummy", ITEM)
        self.imap.uid.assert_not_called()

    def test_mark_read_rejects_a_temporary_only_flag(self):
        self.imap.response.side_effect = [("UIDVALIDITY", [b"77"]), ("PERMANENTFLAGS", [b"(\\Flagged \\*)"])]
        with self.assertRaisesRegex(MailClientError, "永久"):
            self.client.mark_read(ACCOUNT, "dummy", ITEM)
        self.imap.uid.assert_not_called()

    def test_silent_store_is_only_successful_when_target_is_seen(self):
        for confirmation in ([None], [b"1 (UID 87 FLAGS (\\Seen))"], [b"2 (UID 42 FLAGS (\\Flagged))"]):
            self.imap.uid.side_effect = [("OK", [None]), ("OK", confirmation)]
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(MailClientError, "未确认"):
                self.client.mark_read(ACCOUNT, "dummy", ITEM)
        self.imap.reset_mock()
        self.imap.uid.side_effect = [("NO", [b"permission denied"])]
        with self.assertRaises(MailClientError):
            self.client.mark_read(ACCOUNT, "dummy", ITEM)
        self.imap.uid.assert_called_once_with("STORE", "42", "+FLAGS.SILENT", r"(\Seen)")

    def test_body_prefers_chinese_text_over_html_and_omits_attachments(self):
        msg = EmailMessage()
        msg.set_content("你的面试在周一。", charset="gb2312")
        msg.add_alternative("<p>HTML alternative</p>", subtype="html")
        msg.add_attachment("private attachment", filename="attachment.txt")
        raw = msg.as_bytes()
        self.imap.uid.return_value = ("OK", [(b"3 (BODY[]<0> {100}", raw), b" UID 42)"])
        self.assertEqual(self.client.read_body(ACCOUNT, "dummy", ITEM), "你的面试在周一。")
        self.imap.uid.assert_called_with("FETCH", "42", "(UID RFC822.SIZE BODY.PEEK[]<0.2097152>)")

    def test_rich_body_preserves_html_alternative_and_only_its_referenced_inline_images(self):
        msg = EmailMessage()
        msg.set_content("文字替代版", charset="gb2312")
        msg.add_alternative('<p><b>内容</b></p><img src="cid:cover%40mail"><img src="icons/mark.png">',
                            subtype="html", charset="gb2312")
        related = msg.get_payload()[1]
        related.add_related(b"cover image", maintype="image", subtype="jpeg", cid="<cover@mail>",
                            filename="cover.jpg", disposition="inline")
        related.add_related(b"mark image", maintype="image", subtype="png")
        related["Content-Base"] = "https://img.example/"
        related.get_payload()[0]["Content-Location"] = "issue/mail.html"
        related.get_payload()[2]["Content-Location"] = "https://img.example/issue/icons/mark.png"
        msg.add_attachment(b"unrelated image", maintype="image", subtype="jpeg", filename="photo.jpg",
                           cid="<cover@mail>")
        raw = msg.as_bytes()
        self.imap.uid.return_value = ("OK", [(b"3 (UID 42 BODY[]<0> {123}", raw), b")"])
        body = self.client.read_content(ACCOUNT, "dummy", ITEM)
        self.assertEqual(body.text, "文字替代版")
        self.assertEqual(body.base_url, "https://img.example/issue/mail.html")
        self.assertIn("<b>内容</b>", body.html)
        self.assertEqual(body.inline_images, {"cid:cover%40mail": b"cover image", "icons/mark.png": b"mark image"})
        self.imap.select.assert_called_once_with("INBOX", readonly=True)
        self.assertEqual(self.imap.uid.call_count, 1)

    def test_image_only_html_and_single_inline_image_are_readable(self):
        msg = EmailMessage()
        msg.set_content('<img src="https://example.com/cover.jpg" alt="海报">', subtype="html")
        self.imap.uid.return_value = ("OK", [(b"3 (UID 42 BODY[] {50}", msg.as_bytes())])
        body = self.client.read_content(ACCOUNT, "dummy", ITEM)
        self.assertIn("<img", body.html)
        self.assertTrue(body.text)
        self.assertFalse(body.inline_images)
        image = EmailMessage()
        image.set_content(b"inline image bytes", maintype="image", subtype="png")
        self.imap.uid.return_value = ("OK", [(b"3 (UID 42 BODY[] {50}", image.as_bytes())])
        body = self.client.read_content(ACCOUNT, "dummy", ITEM)
        self.assertEqual(list(body.inline_images.values()), [b"inline image bytes"])
        self.assertIn(next(iter(body.inline_images)), body.html)

    def test_large_single_inline_image_fetches_only_remaining_encoded_bytes(self):
        image = EmailMessage()
        payload = b"\xff\xd8\xff" + b"x" * 1_700_000 + b"\xff\xd9"
        image.set_content(payload, maintype="image", subtype="jpeg")
        wire = image.as_bytes()
        start = 2 * 1024 * 1024
        remaining = len(wire) - start
        self.assertGreater(remaining, 0)
        self.imap.uid.side_effect = [
            ("OK", [(f"3 (UID 42 RFC822.SIZE {len(wire)} BODY[]<0> {{{start}}}".encode(), wire[:start])]),
            ("OK", [(f"3 (UID 42 BODY[]<{start}> {{{remaining}}}".encode(), wire[start:])]),
        ]
        body = self.client.read_content(ACCOUNT, "dummy", ITEM)
        self.assertEqual(list(body.inline_images.values()), [payload])
        self.imap.uid.assert_called_with("FETCH", "42", f"(UID BODY.PEEK[]<{start}.{remaining}>)")

    def test_truncated_inline_image_is_replaced_only_with_complete_mime_section(self):
        root = (b"From: original@example.org\r\nContent-Type: multipart/related; boundary=r\r\n\r\n"
                b"--r\r\nContent-Type: text/html\r\n\r\n"
                b'<img src="cid:poster">\r\n--r\r\nContent-Type: image/png\r\nContent-ID: <poster>\r\n'
                b"Content-Transfer-Encoding: base64\r\n\r\nYWJj")
        parts = {
            "": root, "1.MIME": b"Content-Type: text/html\r\n\r\n",
            "2.MIME": b"Content-Type: image/png\r\nContent-ID: <poster>\r\nContent-Transfer-Encoding: base64\r\n\r\n",
            "3.MIME": b"Content-Type: application/pdf\r\nContent-Disposition: attachment\r\n\r\n",
        }
        def fetch(command, uid, fields):
            self.assertIn("BODY.PEEK[", fields)
            section = fields.split("BODY.PEEK[", 1)[1].split("]", 1)[0]
            self.assertNotIn(section, ("1", "3"))  # The HTML was already present; PDF stays on the server.
            if section not in parts:
                return "OK", []
            return "OK", [(b"3 (UID 42 RFC822.SIZE 9000000 BODY[]<0> {123}", parts[section])]

        self.imap.uid.side_effect = fetch
        self.assertFalse(self.client.read_content(ACCOUNT, "dummy", ITEM).inline_images)
        parts["2"] = b"YWJjZGVm"
        body = self.client.read_content(ACCOUNT, "dummy", ITEM)
        self.assertEqual(body.inline_images, {"cid:poster": b"abcdef"})
        self.assertEqual(body.reply.recipient, "original@example.org")
        self.assertIn("<img", body.html)
        self.assertIn("邮件较大", body.text)

    def test_text_attachment_marked_only_by_filename_does_not_hide_html_body(self):
        msg = EmailMessage()
        msg.make_mixed()
        attachment = EmailMessage()
        attachment.set_content("the attachment")
        attachment.set_param("name", "attachment.txt")
        msg.attach(attachment)
        body = EmailMessage()
        body.set_content("<head><title>title</title></head><script>code</script><p>Hello &amp; welcome</p><p>正文</p>", subtype="html")
        msg.attach(body)
        self.imap.uid.return_value = ("OK", [(b"3 (UID 42 RFC822.SIZE 9000000 BODY[]<0> {100}", msg.as_bytes()), b")"])
        text = self.client.read_body(ACCOUNT, "dummy", ITEM)
        self.assertTrue(text.startswith("Hello & welcome\n正文"))
        self.assertNotIn("attachment", text)
        self.assertIn("2 MiB", text)

    def test_deleted_message_produces_readable_error(self):
        self.imap.uid.return_value = ("OK", [None])
        with self.assertRaisesRegex(MailClientError, "删除"):
            self.client.read_body(ACCOUNT, "dummy", ITEM)

    def test_late_html_body_after_large_attachment_fetches_only_its_section(self):
        root = (b"Content-Type: multipart/mixed; boundary=m\r\n\r\n"
                b"--m\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment\r\n\r\nabc")
        parts = {
            "": root,
            "1.MIME": b"Content-Type: application/pdf\r\nContent-Disposition: attachment\r\n\r\n",
            "2.MIME": b"Content-Type: multipart/alternative; boundary=a\r\n\r\n",
            "2.1.MIME": b"Content-Type: text/html; charset=gb2312\r\n\r\n",
            "2.1": "<p>邮件正文在附件后</p>".encode("gb2312"),
        }
        def fetch(command, uid, fields):
            section = fields.split("BODY.PEEK[", 1)[1].split("]", 1)[0]
            return "OK", [(b"3 (UID 42 RFC822.SIZE 9000000", parts[section]), b")"]

        self.imap.uid.side_effect = fetch
        self.assertEqual(self.client.read_body(ACCOUNT, "dummy", ITEM), "邮件正文在附件后")
        self.assertEqual(self.imap.uid.call_count, len(parts))

    def test_long_plain_body_retains_preview_and_shows_truncation(self):
        size = 2 * 1024 * 1024
        header = b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        raw = header + b"x" * (size - len(header))
        self.imap.uid.return_value = ("OK", [(b"3 (UID 42 RFC822.SIZE 8000000", raw), b")"])
        text = self.client.read_body(ACCOUNT, "dummy", ITEM)
        self.assertEqual(text.count("x"), size - len(header))
        self.assertIn("2 MiB", text)
        self.assertEqual(self.imap.uid.call_count, 1)

    @patch("maildesk.mail_client.smtplib.SMTP_SSL")
    def test_server_failure_never_exposes_secret(self, smtp):
        secret = "secret-never-display"
        sender = smtp.return_value
        sender.login.side_effect = smtplib.SMTPAuthenticationError(535, secret.encode())
        with self.assertRaisesRegex(MailClientError, "SMTP") as error:
            self.client.verify(ACCOUNT, secret)
        self.assertNotIn(secret, str(error.exception))
        self.imap.login.side_effect = imaplib.IMAP4.error(secret)
        with self.assertRaisesRegex(MailClientError, "IMAP") as error:
            self.client.verify(ACCOUNT, secret)
        self.assertNotIn(secret, str(error.exception))
