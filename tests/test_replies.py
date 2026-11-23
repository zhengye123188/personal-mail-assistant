import smtplib
import tempfile
import unittest
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import ANY, call, patch

from maildesk.demo import DemoVault
from maildesk.mail_client import MailClient, MailClientError, ReplyDeliveryUncertain, _parse_body
from maildesk.models import Account, ReplyEnvelope
from maildesk.service import MailService
from tests.mail_fixtures import ACCOUNTS, FixtureClient


class ReplyTests(unittest.TestCase):
    def test_reply_to_header_and_thread_metadata_come_from_outer_message(self):
        mail = EmailMessage()
        mail["From"] = "Notifications <noreply@example.org>"
        mail["Reply-To"] = "项目组 <review+42@example.org>"
        mail["Subject"] = "Re: 审阅申请"
        mail["Message-ID"] = "<thread/answer+42@example.org>"
        mail["References"] = "<root@example.org> <comment@example.org>"
        mail.set_content("请审阅")
        body = _parse_body(mail.as_bytes(), rich=True)
        self.assertEqual(body.reply, ReplyEnvelope(
            "review+42@example.org", "Re: 审阅申请", "<thread/answer+42@example.org>",
            ("<root@example.org>", "<comment@example.org>", "<thread/answer+42@example.org>"),
        ))
        self.assertIsNone(_parse_body(mail.as_bytes()).reply)
        del mail["Reply-To"]
        del mail["References"]
        mail["In-Reply-To"] = "<root@example.org>"
        mail.replace_header("Subject", "讨论")
        reply = _parse_body(mail.as_bytes(), rich=True).reply
        self.assertEqual(reply.recipient, "noreply@example.org")
        self.assertEqual(reply.subject, "Re: 讨论")
        self.assertEqual(reply.references, ("<root@example.org>", "<thread/answer+42@example.org>"))

    def test_ambiguous_or_malformed_reply_headers_leave_body_readable(self):
        for value in (b"Jane", b"Jane <jane>", b"", b"a@example.org, b@example.org",
                      b"a@example.org\r\nReply-To: b@example.org"):
            with self.subTest(value=value):
                raw = b"From: fallback@example.org\r\nReply-To: " + value + b"\r\n\r\nHello"
                body = _parse_body(raw, rich=True)
                self.assertEqual(body.text, "Hello")
                self.assertIsNone(body.reply)

    def test_raw_chinese_display_name_does_not_hide_a_valid_reply_address(self):
        for charset in ("gbk", "utf-8"):
            raw = "From: 小林 <lin@example.org>\r\n\r\nHello".encode(charset)
            self.assertEqual(_parse_body(raw, rich=True).reply.recipient, "lin@example.org")
        incomplete = b"From: a@example.org\r\nReply-To: still@partial"
        self.assertIsNone(_parse_body(incomplete, rich=True, partial=True).reply)

    @patch("maildesk.mail_client.smtplib.SMTP_SSL")
    def test_only_the_confirmed_recipient_receives_the_edited_threaded_reply(self, smtp):
        client = MailClient()
        reply = ReplyEnvelope("review+42@example.org", "Re: 审阅", "<tip@example.org>",
                              ("<root@example.org>", "<tip@example.org>"))
        connection = smtp.return_value
        connection.send_message.return_value = {}
        connection.quit.side_effect = smtplib.SMTPServerDisconnected("closed after acceptance")
        accounts = (*ACCOUNTS, Account("reader@gmail.com", "gmail"))
        for account in accounts:
            with self.subTest(account=account):
                client.send_reply(account, "test-code", reply, "我编辑过的回复。")
                self.assertEqual(smtp.call_args.args, (f"smtp.{account.provider}.com", 465))
                self.assertTrue(smtp.call_args.kwargs["context"].check_hostname)
                connection.login.assert_called_with(account.address, "test-code")
                args, kwargs = connection.send_message.call_args
                self.assertEqual(kwargs, {"from_addr": account.address, "to_addrs": [reply.recipient]})
                sent, = args
                self.assertEqual(sent["From"], account.address)
                self.assertEqual(sent["To"], reply.recipient)
                self.assertEqual(sent["Subject"], reply.subject)
                self.assertEqual(sent["In-Reply-To"], reply.in_reply_to)
                self.assertEqual(sent["References"], " ".join(reply.references))
                self.assertIn("@", sent["Message-ID"])
                self.assertIn("Date", sent)
                self.assertIsNone(sent["Cc"])
                self.assertEqual(sent.get_content().strip(), "我编辑过的回复。")
        self.assertEqual(connection.send_message.call_count, len(accounts))

    @patch("maildesk.mail_client.smtplib.SMTP")
    def test_icloud_reply_upgrades_before_authenticating_and_submits_once(self, smtp):
        account = Account("reader@icloud.com", "icloud")
        reply = ReplyEnvelope("review@example.org", "Re: Review")
        connection = smtp.return_value
        connection.send_message.return_value = {}
        connection.quit.side_effect = smtplib.SMTPServerDisconnected("closed after acceptance")
        MailClient().send_reply(account, "test-code", reply, "Hello")
        self.assertEqual(smtp.call_args.args, ("smtp.mail.me.com", 587))
        self.assertEqual(connection.method_calls, [
            call.starttls(context=ANY), call.login(account.address, "test-code"),
            call.send_message(ANY, from_addr=account.address, to_addrs=[reply.recipient]),
            call.quit(), call.close(),
        ])

    @patch("maildesk.mail_client.smtplib.SMTP_SSL")
    def test_refusal_and_lost_ack_are_distinct_and_do_not_retry(self, smtp):
        account = ACCOUNTS[0]
        client = MailClient()
        reply = ReplyEnvelope("review@example.org", "Re: Review")
        connection = smtp.return_value
        connection.send_message.side_effect = smtplib.SMTPDataError(550, b"private detail")
        with self.assertRaisesRegex(MailClientError, "未接受") as rejected:
            client.send_reply(account, "test-code", reply, "Hello")
        self.assertNotIsInstance(rejected.exception, ReplyDeliveryUncertain)
        connection.send_message.side_effect = smtplib.SMTPServerDisconnected("private detail")
        with self.assertRaisesRegex(ReplyDeliveryUncertain, "发送结果未知"):
            client.send_reply(account, "test-code", reply, "Hello")
        connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private detail")
        with self.assertRaisesRegex(MailClientError, "SMTP 登录失败"):
            client.send_reply(account, "test-code", reply, "Hello")
        self.assertEqual(connection.send_message.call_count, 2)

    def test_reply_bound_to_message_and_account_and_demo_delivery_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FixtureClient()
            service = MailService(Path(tmp) / "accounts.db", client, DemoVault())
            qq, netease = ACCOUNTS
            for account in ACCOUNTS:
                service.bind(account.provider, account.address, "demo")
            item = service.list_messages(qq).items[0]
            reply = service.read_content(qq, item).reply
            with self.assertRaises(ValueError):
                service.send_reply(netease, item, reply, "wrong account")
            with self.assertRaises(ValueError):
                service.send_reply(netease, service.list_messages(netease).items[0], reply, "shared uid")
            with self.assertRaises(ValueError):
                service.send_reply(qq, replace(item, validity="2"), reply, "reused uid")
            with self.assertRaises(ValueError):
                service.send_reply(qq, item, reply, "  \n")
            self.assertEqual(client.sent_replies, [])
            service.send_reply(qq, item, reply, "演示回复")
            self.assertEqual(len(client.sent_replies), 1)
            self.assertEqual(client.sent_replies[0]["From"], qq.address)
            self.assertEqual(client.sent_replies[0]["To"], reply.recipient)
