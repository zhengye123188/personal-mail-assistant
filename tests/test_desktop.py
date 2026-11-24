import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton

from maildesk.models import Account, InboxCursor, MailBody, MailPage, PAGE_SIZE
from maildesk.providers import MAIL_PROVIDERS
from maildesk.reply_ai import ReplyAI
from maildesk.service import CredentialAccessPaused, MailboxAuthorizationRequired, MailService
from maildesk.ui import MainWindow
from tests.mail_fixtures import MemoryVault, ACCOUNTS, FixtureClient, PNG


class DelayedInbox(FixtureClient):
    def __init__(self):
        super().__init__()
        self.qq_started = threading.Event()
        self.release_qq = threading.Event()
        self.unavailable = set()

    def list_messages(self, account, secret, before_uid=None):
        if account.address in self.unavailable:
            raise ConnectionError("模拟连接失败")
        return super().list_messages(account, secret, before_uid)

    def read_body(self, account, secret, item):
        if account.provider == "qq":
            self.qq_started.set()
            if not self.release_qq.wait(4):
                raise TimeoutError("模拟请求超时")
        return super().read_body(account, secret, item)

    def scan_arrivals(self, account, secret, validity, after_uid):
        if account.address in self.unavailable:
            raise ConnectionError("模拟提醒检查失败")
        return super().scan_arrivals(account, secret, validity, after_uid)


class DesktopTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = DelayedInbox()
        self.service = MailService(Path(directory.name) / "accounts.db", self.client, MemoryVault())
        settings = QSettings(str(Path(directory.name) / "models.ini"), QSettings.Format.IniFormat)
        self.enterContext(patch("maildesk.ui.QSettings", return_value=settings))
        self.enterContext(patch.dict(os.environ, DEEPSEEK_API_KEY="fixture", DASHSCOPE_API_KEY="fixture"))
        self.provider = self.enterContext(patch("maildesk.reply_ai._request",
                                                side_effect=AssertionError("本次操作不应请求模型")))
        self.window = MainWindow(self.service)
        self.window.show()

    def tearDown(self):
        self.client.release_qq.set()
        self.wait_until(lambda: not self.window.jobs)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, predicate):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("操作未在预期时间内完成")

    def bind_in_ui(self, account, secret="offline-test-code"):
        self.window.findChild(QPushButton, "bindButton").click()
        dialog = self.window.binding
        local, _, domain = account.address.partition("@")
        dialog.domain.setCurrentText("@" + domain)
        dialog.address.setText(local)
        dialog.secret.setText(secret)
        dialog.submit.click()
        self.wait_until(lambda: self.window.binding is None and self.window.mail_list.count() == 3)

    def show_mailbox(self, kind, address=None):
        from maildesk.ui import NAV_KIND, USER_ROLE

        nav = self.window.account_list
        for row in range(nav.count()):
            item = nav.item(row)
            if item.data(NAV_KIND) == kind and item.data(USER_ROLE) == address:
                nav.setCurrentItem(item)
                return
        self.fail(f"侧栏缺少邮箱 {kind} {address}")

    def click_message(self, row):
        listing = self.window.mail_list
        position = listing.visualItemRect(listing.item(row)).center()
        QTest.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=position)

    def open_first_reply(self, account):
        self.show_mailbox("account", account.address)
        self.wait_until(lambda: self.window.mail_list.count() == 3 and not self.window.jobs)
        self.click_message(0)
        self.wait_until(lambda: self.window.reply_button.isEnabled() and not self.window.jobs)
        item = self.window.mail_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertEqual(item.account, account.address)
        key = (item.account, item.validity, item.uid)
        self.window.reply_button.click()
        self.wait_until(lambda: key in self.window.reply_drafts and self.window.reply_drafts[key].dialog.isVisible())
        return key, self.window.reply_drafts[key].dialog

    def set_ai_enabled(self, enabled):
        self.window.ai_settings_button.click()
        dialog = self.window.model_settings
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.enabled.isChecked(), not enabled)
        dialog.enabled.setChecked(enabled)
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.wait_until(lambda: self.window.model_settings is None)

    def model_replies(self, *values):
        self.provider.side_effect = [
            {"choices": [{"message": {"content": value if isinstance(value, str) else json.dumps(value)}}]}
            for value in values
        ]

    def open_chat(self):
        self.client.release_qq.set()
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        self.show_mailbox("account", ACCOUNTS[0].address)
        self.wait_until(lambda: not self.window.jobs)
        self.assertFalse(self.window.chat_button.isVisible())
        self.set_ai_enabled(True)
        self.assertTrue(self.window.chat_button.isVisible())
        self.window.chat_button.click()
        chat = self.window.chat_dialog
        self.assertTrue(chat.isVisible())
        return chat

    def ask_chat(self, chat, question):
        previous = len(chat.history)
        chat.input.setPlainText(question)
        chat.send_button.click()
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(len(chat.history), previous + 1, chat.status.text())
        return chat.history[-1]


class DesktopFlowTests(DesktopTestCase):
    def test_draft_folder_restores_an_offline_reply_after_restarting(self):
        qq = ACCOUNTS[0]
        self.client.release_qq.set()
        self.bind_in_ui(qq)
        key, dialog = self.open_first_reply(qq)
        dialog.draft.setPlainText("尚未发送的回复")
        dialog.instructions.setPlainText("请明确抵达时间")
        dialog.close()
        self.assertTrue(self.window.close())
        self.window.deleteLater()
        self.client.unavailable.add(qq.address)
        self.window = MainWindow(self.service)
        self.window.show()
        self.wait_until(lambda: not self.window.jobs)
        with patch.object(self.client, "read_content", side_effect=AssertionError("不应读取 IMAP")) as read:
            self.show_mailbox("drafts")
            self.assertEqual(self.window.mail_list.count(), 1)
            self.click_message(0)
            self.assertEqual(self.window.body.toPlainText(), "尚未发送的回复")
            self.window.reply_button.click()
            restored = self.window.reply_drafts[key].dialog
            self.assertTrue(restored.isVisible())
            self.assertEqual(restored.draft.toPlainText(), "尚未发送的回复")
            self.assertEqual(restored.instructions.toPlainText(), "请明确抵达时间")
            read.assert_not_called()
        with patch.object(self.client, "send_reply", side_effect=ConnectionError("服务器暂不可用")):
            restored.send_button.click()
            self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.service.list_drafts()[0].text, "尚未发送的回复")
        self.assertIn("服务器暂不可用", restored.status.text())
        self.assertFalse(self.client.sent_replies)

    def test_draft_folder_confirms_discard_and_cancels_delayed_saves(self):
        from PySide6.QtWidgets import QMessageBox

        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        key, dialog = self.open_first_reply(account)
        dialog.draft.setPlainText("需要保留的草稿")
        self.show_mailbox("drafts")
        self.click_message(0)
        self.window.discard_button.click()
        prompt = self.window.findChild(QMessageBox, "discardDraftPrompt")
        prompt.defaultButton().click()
        self.assertEqual(len(self.service.list_drafts()), 1)
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        dialog.draft.setPlainText("删除前的最后一次修改")
        self.window.discard_button.click()
        prompt = self.window.findChild(QMessageBox, "discardDraftPrompt")
        next(button for button in prompt.buttons() if button.text() == "删除草稿").click()
        self.assertNotIn(key, self.window.reply_drafts)
        self.assertEqual(self.window.mail_list.count(), 0)
        QTest.qWait(400)
        self.assertEqual(self.service.list_drafts(), [])

    def test_manual_reply_keeps_separate_drafts_for_same_uid_and_sends_from_original_account(self):
        self.client.release_qq.set()
        qq, netease = ACCOUNTS
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        first_key, first = self.open_first_reply(qq)
        self.assertFalse(first.generate_button.isVisible())
        first.draft.setPlainText("周五我会提前到阅览室。")
        first.close()

        second_key, second = self.open_first_reply(netease)
        self.assertEqual(first_key[1:], second_key[1:])
        self.assertEqual(second.draft.toPlainText(), "")
        second.draft.setPlainText("评审会议的时间收到了。")
        second.close()

        _, first = self.open_first_reply(qq)
        self.assertEqual(first.draft.toPlainText(), "周五我会提前到阅览室。")
        first.close()
        _, second = self.open_first_reply(netease)
        self.assertEqual(second.draft.toPlainText(), "评审会议的时间收到了。")
        self.assertEqual(self.client.sent_replies, [])
        second.draft.setPlainText("周三下午两点见。")
        self.show_mailbox("account", qq.address)
        self.wait_until(lambda: not self.window.jobs)
        second.send_button.click()
        self.wait_until(lambda: len(self.client.sent_replies) == 1 and not self.window.jobs)
        sent = self.client.sent_replies[0]
        self.assertEqual(sent["From"], netease.address)
        self.assertEqual(sent["To"], "reviewer@example.org")
        self.assertEqual(sent.get_content().strip(), "周三下午两点见。")
        self.assertIn(first_key, self.window.reply_drafts)
        self.assertEqual([d.item.account for d in self.service.list_drafts()], [qq.address])
        self.provider.assert_not_called()

    def test_toggle_ai_in_settings_updates_open_draft_and_generated_text_can_be_sent_manually(self):
        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        _, composer = self.open_first_reply(account)
        composer.draft.setPlainText("先记下我的回复")
        self.assertFalse(composer.generate_button.isVisible())
        self.model_replies({"kind": "draft", "draft": "可以提供下周的会议议程吗？"})
        self.set_ai_enabled(True)
        self.assertTrue(composer.generate_button.isVisible())
        self.assertEqual(composer.draft.toPlainText(), "先记下我的回复")
        composer.instructions.setPlainText("请对方补充信息")
        composer.generate_button.click()
        self.wait_until(lambda: composer.draft.toPlainText() != "先记下我的回复" and not self.window.jobs)
        generated = composer.draft.toPlainText()
        self.assertTrue(generated.strip())
        self.assertEqual(self.client.sent_replies, [])
        self.set_ai_enabled(False)
        self.assertFalse(composer.generate_button.isVisible())
        self.assertFalse(composer.instructions.isVisible())
        self.assertEqual(composer.draft.toPlainText(), generated)
        composer.draft.setPlainText("请补充下周的会议议程，谢谢。")
        self.assertTrue(composer.send_button.isEnabled())
        composer.send_button.click()
        self.wait_until(lambda: len(self.client.sent_replies) == 1 and not self.window.jobs)
        self.assertEqual(self.client.sent_replies[0].get_content().strip(), "请补充下周的会议议程，谢谢。")
        self.provider.assert_called_once()

    def test_chinese_instructions_draft_and_shorten_an_english_reply_from_the_reader(self):
        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        self.set_ai_enabled(True)
        original = ("Hi Chen, could you confirm receipt of the agenda for our next meeting?\n\n"
                    "-----Original Message-----\n" + "这里是之前用中文讨论的会议安排。\n" * 12)
        self.model_replies({"kind": "draft", "draft": "Hi, I have received the agenda. Thank you."},
                           {"kind": "draft", "draft": "Agenda received, thanks."})
        read_content = self.client.read_content

        def read_english(*args):
            return replace(read_content(*args), text=original)

        with patch.object(self.client, "read_content", side_effect=read_english):
            _, dialog = self.open_first_reply(account)
            self.assertIn("could you confirm receipt", self.window.body.toPlainText())
            dialog.instructions.setPlainText("确认收到")
            dialog.generate_button.click()
            self.wait_until(lambda: not self.window.jobs)
            first = dialog.draft.toPlainText()
            self.assertIn("received", first.lower(), dialog.status.text())
            self.assertNotRegex(first, r"[\u3400-\u9fff]")

            dialog.instructions.setPlainText("再简短一点")
            dialog.generate_button.click()
            self.wait_until(lambda: not self.window.jobs)
            revised = dialog.draft.toPlainText()
            self.assertIn("received", revised.lower(), dialog.status.text())
            self.assertNotRegex(revised, r"[\u3400-\u9fff]")
            self.assertLess(len(revised), len(first))
            context = json.loads(self.provider.call_args.args[1]["messages"][-1]["content"])
            self.assertEqual(context["编辑器内的草稿"], first)
            self.assertEqual(context["回复语言"]["target"], "en")
            self.assertFalse(self.client.sent_replies)
            dialog.draft.clear()
            dialog.close()

    def test_multiline_reply_requirements_reach_provider_from_reopened_composer(self):
        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        model = ReplyAI(provider="deepseek", key_lookup=lambda: "test-only")
        requirements = "先谢谢对方邀请。\n周三下午不方便，问问能不能改到周五上午。\n语气礼貌一点，两三句话。"
        reply = "谢谢您的邀请！周三下午我不方便参加，能否改到周五上午？"
        answer = {"kind": "draft", "answer": "已按要求写好。", "draft": reply}
        with patch("maildesk.ui.selected_model", return_value=model), patch(
                "maildesk.reply_ai._request", return_value={"choices": [{"message": {"content": json.dumps(answer)}}]}
        ) as provider:
            self.set_ai_enabled(True)
            _, composer = self.open_first_reply(account)
            composer.instructions.setPlainText(requirements)
            composer.close()
            _, composer = self.open_first_reply(account)
            provider.assert_not_called()
            self.assertEqual(composer.instructions.toPlainText(), requirements)
            composer.generate_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(composer.draft.toPlainText(), reply)
            self.assertEqual(provider.call_count, 1)
            messages = provider.call_args.args[1]["messages"]
            self.assertEqual(json.loads(messages[-1]["content"])["本次要求"], requirements)
            self.assertIn("评审", messages[-1]["content"])
            self.assertEqual(self.client.sent_replies, [])
            composer.draft.clear()
            composer.close()

    def test_tab_accepts_the_selected_mails_reply_suggestion_without_drafting(self):
        qq, netease = ACCOUNTS
        self.client.release_qq.set()
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        model = ReplyAI(provider="deepseek", key_lookup=lambda: "test-only")
        result = {"kind": "draft", "answer": "已拟好。", "draft": "你好，请问评审将在哪里举行？"}
        response = {"choices": [{"message": {"content": json.dumps(result)}}]}
        with patch("maildesk.ui.selected_model", return_value=model), \
                patch("maildesk.reply_ai._request", return_value=response) as provider:
            self.set_ai_enabled(True)

            def open_and_complete(account):
                _, dialog = self.open_first_reply(account)
                self.assertEqual(dialog.instructions.toPlainText(), "")
                self.assertFalse(dialog.generate_button.isEnabled())
                hint = dialog.instructions.placeholderText()
                dialog.instructions.setFocus()
                QTest.keyClick(dialog.instructions, Qt.Key.Key_Tab)
                self.assertEqual(dialog.instructions.toPlainText(), hint)
                self.assertTrue(dialog.generate_button.isEnabled())
                self.assertEqual(dialog.draft.toPlainText(), "")
                self.assertFalse(dialog.assistant_history)
                self.assertFalse(self.window.jobs)
                provider.assert_not_called()
                return dialog, dialog.instructions.toPlainText()

            first, qq_suggestion = open_and_complete(qq)
            self.assertRegex(qq_suggestion, "提前|十分钟|布置|阅览室")
            handwritten = "我的交通时间还没确定，请问最晚几点到？"
            first.instructions.setPlainText(handwritten)
            first.close()

            second, netease_suggestion = open_and_complete(netease)
            self.assertRegex(netease_suggestion, "周三|两点|2点")
            self.assertNotEqual(qq_suggestion, netease_suggestion)
            second.generate_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(second.draft.toPlainText(), result["draft"])
            self.assertEqual(provider.call_count, 1)
            context = json.loads(provider.call_args.args[1]["messages"][-1]["content"])
            self.assertEqual(context["本次要求"], netease_suggestion)
            self.assertFalse(self.client.sent_replies)
            second.draft.clear()
            second.close()

            _, first = self.open_first_reply(qq)
            self.assertEqual(first.instructions.toPlainText(), handwritten)
            self.assertEqual(first.draft.toPlainText(), "")
            first.instructions.clear()
            first.close()

    def test_journal_notification_tab_completes_in_reply_and_restored_draft(self):
        account = ACCOUNTS[1]
        subject = "Journal of Cloud Computing submission update"
        body = ("Please upload the COI-17 disclosure form in the submission portal.\n"
                "Please do not reply to this email.")
        self.client._examples["163"] = ((3, 1, "Editorial Office", subject, body),
                                        *self.client._examples["163"][1:])
        self.client._senders["Editorial Office"] = "do-not-reply"
        self.bind_in_ui(account)
        self.set_ai_enabled(True)
        key, dialog = self.open_first_reply(account)

        def accept_suggestion(composer):
            self.assertTrue(composer.completion_hint.isVisible())
            self.assertTrue(composer.model_completion.isVisible())
            self.assertFalse(composer.model_completion.isChecked())
            field = composer.instructions
            self.assertEqual(field.toPlainText(), "")
            hint = field.placeholderText()
            self.assertIn("COI-17", hint)
            field.setFocus()
            QTest.keyClick(field, Qt.Key.Key_Tab)
            self.assertEqual(field.toPlainText(), hint)
            self.assertTrue(composer.generate_button.isEnabled())
            self.provider.assert_not_called()
            self.assertFalse(self.client.sent_replies)

        accept_suggestion(dialog)
        self.assertEqual(dialog.draft.toPlainText(), "")
        dialog.instructions.clear()
        dialog.draft.setPlainText("Handwritten draft")
        self.assertTrue(self.window.close())
        self.window.deleteLater()
        self.window = MainWindow(self.service)
        self.window.show()
        self.wait_until(lambda: not self.window.jobs)
        self.show_mailbox("drafts")
        self.click_message(0)
        self.window.reply_button.click()
        restored = self.window.reply_drafts[key].dialog
        accept_suggestion(restored)
        self.assertEqual(restored.draft.toPlainText(), "Handwritten draft")
        restored.model_completion.click()
        self.assertTrue(restored.model_completion.isChecked())
        self.provider.assert_not_called()

    def test_mail_chat_followup_opens_reply_with_original_context_and_revises_manual_edits(self):
        qq, netease = ACCOUNTS
        chat = self.open_chat()
        self.model_replies(
            {"action": "search"}, {"parts": [{"text": "两封邮件需要确认。", "sources": [1, 2]}]},
            {"action": "draft"}, "请问设计评审会议在哪里举行？",
            {"kind": "answer", "answer": "对方请你确认评审时间。"},
            {"kind": "draft", "draft": "周三不方便参加，感谢邀请。"},
            {"kind": "draft", "draft": "周四上午十点见，请带蓝色样稿。"},
        )
        listed = self.ask_chat(chat, "列出两个邮箱最近的邮件")
        self.assertEqual({m.account for m in listed.sources}, {qq.address, netease.address})
        chosen = listed.sources[1]
        self.assertEqual(chosen.account, netease.address)
        key = (chosen.account, chosen.validity, chosen.uid)
        draft = self.ask_chat(chat, "帮我回第二封")
        self.assertEqual(draft.draft_item, chosen)
        self.assertTrue(draft.draft_text)
        self.assertEqual(self.window.active_address, qq.address)
        chat.findChild(QPushButton, "chatCompose").click()
        self.wait_until(lambda: key in self.window.reply_drafts and not self.window.jobs)
        dialog = self.window.reply_drafts[key].dialog
        self.assertTrue(dialog.isVisible())
        header_values = [f.text() for f in dialog.findChildren(QLineEdit) if f.property("headerValue")]
        self.assertIn(netease.address, header_values)
        self.assertIn("reviewer@example.org", header_values)
        self.assertEqual(dialog.draft.toPlainText(), draft.draft_text)
        self.assertEqual([t.question for t in dialog.assistant_history], [draft.question])
        self.assertNotIn(qq.address, dialog.assistant_transcript.toPlainText())

        def submit_instruction(button, question):
            previous = len(dialog.assistant_history)
            dialog.instructions.setPlainText(question)
            button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(len(dialog.assistant_history), previous + 1, dialog.status.text())
            return dialog.assistant_history[-1]

        answer = submit_instruction(dialog.ask_button, "对方希望我确认什么？")
        self.assertIn("评审", answer.answer)
        self.assertFalse(answer.draft)
        self.assertEqual(dialog.draft.toPlainText(), draft.draft_text)
        generated = submit_instruction(dialog.generate_button, "帮我写一封婉拒的回复")
        self.assertIn("不方便", generated.draft)
        self.assertEqual(dialog.draft.toPlainText(), generated.draft)
        edited = "你好，\n\n谢谢你的来信。周四上午十点我可以参加。请带上蓝色样稿。"
        dialog.draft.setPlainText(edited)
        revised = submit_instruction(dialog.generate_button, "把这段回复改短一点")
        submitted = json.loads(self.provider.call_args.args[1]["messages"][-1]["content"])
        self.assertEqual(submitted["编辑器内的草稿"], edited)
        self.assertIn("评审", submitted["来信"]["主题"])
        self.assertIn("蓝色样稿", revised.draft)
        self.assertLess(len(revised.draft), len(edited))
        self.assertEqual(dialog.draft.toPlainText(), revised.draft)
        self.assertTrue(dialog.send_button.isEnabled())
        self.assertFalse(self.client.sent_replies)
        self.assertTrue(next(m for m in self.service.list_messages(netease).items if m.uid == chosen.uid).unread)
        dialog.draft.clear()
        dialog.close()

    def test_chat_drafts_save_before_editing_and_preserve_later_manual_changes(self):
        chat = self.open_chat()
        self.model_replies(
            {"action": "search"}, {"parts": [{"text": "以下邮件等待回复。", "sources": [1, 2]}]},
            {"action": "draft"}, "收到下周的安排，我看看时间后再确认。",
            {"action": "revise"}, "待确认时间。",
            {"action": "revise"}, "我会确认时间后再回复。",
        )
        self.ask_chat(chat, "最近有哪些邮件需要回复？")
        first = self.ask_chat(chat, "帮我回第二封")
        self.assertEqual(self.service.list_drafts()[0].text, first.draft_text)
        latest = self.ask_chat(chat, "再简短一点")
        self.assertNotEqual(first.draft_text, latest.draft_text)
        self.assertEqual(self.service.list_drafts()[0].text, latest.draft_text)
        with patch.object(self.window, "_run") as worker:
            cards = chat.findChildren(QPushButton, "chatCompose")
            cards[0].click()
            cards[1].click()
            worker.assert_not_called()
        self.assertEqual(len(self.window.reply_drafts), 1)
        dialog = next(iter(self.window.reply_drafts.values())).dialog
        self.assertEqual(dialog.draft.toPlainText(), latest.draft_text)
        self.assertEqual(dialog.assistant_history[-1].question, latest.question)
        dialog.draft.setPlainText("我自己补充的时间，请保留。")
        self.ask_chat(chat, "修改草稿，换个说法")
        self.assertEqual(dialog.draft.toPlainText(), "我自己补充的时间，请保留。")
        dialog.close()
        self.assertEqual(self.service.list_drafts()[0].text, "我自己补充的时间，请保留。")
        self.assertFalse(self.client.sent_replies)
        dialog.draft.clear()
        dialog.close()

    def test_late_chat_revision_does_not_restore_a_discarded_draft(self):
        from PySide6.QtWidgets import QMessageBox

        chat = self.open_chat()
        self.model_replies(
            {"action": "search"}, {"parts": [{"text": "以下邮件等待回复。", "sources": [1, 2]}]},
            {"action": "draft"}, "收到下周的安排，我看看时间后再确认。",
            {"action": "revise"}, "待确认时间。",
        )
        self.ask_chat(chat, "最近有哪些邮件需要回复？")
        self.ask_chat(chat, "帮我回第二封")
        jobs = []
        with patch.object(chat, "_run", side_effect=lambda work, done: jobs.append((work, done))):
            chat.input.setPlainText("再简短一点")
            chat.send_button.click()
        work, done = jobs.pop()
        latest = work()
        self.show_mailbox("drafts")
        self.click_message(0)
        self.window.discard_button.click()
        prompt = self.window.findChild(QMessageBox, "discardDraftPrompt")
        next(button for button in prompt.buttons() if button.text() == "删除草稿").click()
        done(latest, None)
        self.assertTrue(chat.history[-1].draft_text)
        self.assertEqual(self.window.mail_list.count(), 0)
        self.assertEqual(self.service.list_drafts(), [])
        self.assertIn("移出草稿箱", chat.status.text())

    def test_chat_source_opens_cross_account_mail_beyond_first_page(self):
        qq, netease = ACCOUNTS
        chat = self.open_chat()
        for _ in range(PAGE_SIZE):
            self.client.simulate_arrival(netease)
        self.show_mailbox("all")
        self.window.refresh_button.click()
        self.wait_until(lambda: not self.window.jobs)
        self.model_replies({"action": "search", "text": "校样"},
                           {"parts": [{"text": "有一封校样核对。", "sources": [1]}]})
        result = self.ask_chat(chat, "关于校样的邮件有哪些？")
        self.assertEqual(len(result.sources), 1, result.answer)
        target = result.sources[0]
        self.assertEqual((target.account, target.subject), (netease.address, "校样核对"))
        self.assertNotIn(target.uid, {m.uid for m in self.service.list_messages(netease).items})
        qq_twin = next(m for m in self.service.list_messages(qq).items if m.uid == target.uid)
        self.assertTrue(qq_twin.unread and target.unread)
        with patch.object(self.client, "mark_read", wraps=self.client.mark_read) as mark:
            chat.findChild(QPushButton, "chatSource").click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertFalse(chat.isVisible())
            self.assertEqual(self.window.active_address, netease.address)
            selected = self.window.mail_list.currentItem().data(Qt.ItemDataRole.UserRole)
            self.assertEqual((selected.account, selected.validity, selected.uid),
                             (target.account, target.validity, target.uid))
            self.assertIn("第二段里的作品名称", self.window.body.toPlainText())
            self.assertIn(netease.address, self.window.meta.text())
            self.assertTrue(self.window.older_button.isVisible())
            mark.assert_called_once()
            self.assertEqual(mark.call_args.args[0], netease)
            self.assertEqual(mark.call_args.args[2].uid, target.uid)
        self.assertTrue(next(m for m in self.service.list_messages(qq).items if m.uid == target.uid).unread)
        self.assertFalse(self.client.sent_replies)

    def test_external_images_load_on_open_and_late_download_stays_with_its_message(self):
        self.client.release_qq.set()
        qq, netease = ACCOUNTS
        for account in (qq, netease):
            self.bind_in_ui(account)
        url = "https://images.example.org/meeting.png"
        picture = PNG

        def content(account, secret, item):
            title = "会议安排" if account == qq else "另一封邮件"
            return MailBody(title, f'<p>{title}</p><img src="{url}" alt="路线图">')

        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def fetch(urls, *, cancelled):
            if started.is_set():
                return {}
            started.set()
            release.wait(3)
            finished.set()
            return {url: picture}

        with patch.object(self.client, "read_content", side_effect=content), \
                patch("maildesk.ui.download_images", side_effect=fetch) as download:
            self.show_mailbox("account", qq.address)
            self.wait_until(lambda: self.window.mail_list.count() == 3)
            download.assert_not_called()
            self.click_message(0)
            self.wait_until(started.is_set)
            self.assertIn("会议安排", self.window.body.toPlainText())
            self.assertFalse(finished.is_set())
            self.show_mailbox("account", netease.address)
            self.wait_until(lambda: self.window.mail_list.count() == 3)
            self.click_message(0)
            self.wait_until(lambda: "另一封邮件" in self.window.body.toPlainText())
            self.assertTrue(download.call_args_list[0].kwargs["cancelled"]())
            release.set()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(download.call_count, 2)
            self.assertEqual(self.window.body.remote_urls, (url,))
            self.assertTrue(self.window.reader.load_images.isVisible())
            self.assertIn("另一封邮件", self.window.body.toPlainText())

    def test_all_image_batches_run_automatically_and_retry_only_failed_sources(self):
        qq = ACCOUNTS[0]
        self.bind_in_ui(qq)
        urls = [f"https://images.example.org/map-{i}.png" for i in range(5)]
        picture = PNG
        tags = "".join(f'<img src="{url}" alt="路线图">' for url in [*urls, urls[0]])
        content = MailBody("会议地点", "<p>会议地点</p>" + tags)
        failed = {urls[0], urls[2]}

        def fetch(sources, *, cancelled):
            return {source: picture for source in sources if source not in failed}

        with patch("maildesk.ui.IMAGE_BATCH_SIZE", 2), patch.object(self.client, "read_content", return_value=content), \
                patch("maildesk.ui.download_images", side_effect=fetch) as download:
            self.click_message(0)
            self.wait_until(lambda: download.call_count >= 3 and not self.window.jobs)
            batches = [list(call.args[0]) for call in download.call_args_list]
            self.assertEqual(batches, [urls[:2], urls[2:4], urls[4:]])
            self.assertEqual(set(self.window.body.remote_urls), failed)
            self.assertTrue(self.window.reader.load_images.isVisible())
            self.assertIn("会议地点", self.window.body.toPlainText())

            failed.clear()
            self.window.reader.load_images.click()
            self.wait_until(lambda: download.call_count >= 4 and not self.window.jobs)
            self.assertFalse(self.window.reader.image_notice.isVisible())
            self.assertIn("会议地点", self.window.body.toPlainText())
            self.assertFalse(self.window.body.remote_urls)
            self.assertEqual(download.call_count, 4)
            self.assertEqual(tuple(download.call_args.args[0]), (urls[0], urls[2]))

    def test_bind_switch_and_read_same_uid_while_old_body_is_still_loading(self):
        qq, netease = ACCOUNTS
        self.bind_in_ui(qq)
        self.bind_in_ui(netease)
        self.assertEqual(self.service.accounts(), [qq, netease])
        self.show_mailbox("account", qq.address)
        self.wait_until(lambda: self.window.mail_list.count() == 3)
        self.window.mail_list.setCurrentRow(0)
        self.wait_until(self.client.qq_started.is_set)
        qq_item = self.window.mail_list.currentItem().data(Qt.ItemDataRole.UserRole)

        self.show_mailbox("account", netease.address)
        self.wait_until(lambda: self.window.mail_list.count() == 3)
        self.window.mail_list.setCurrentRow(0)
        self.wait_until(lambda: "设计评审" in self.window.body.toPlainText())
        netease_item = self.window.mail_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertEqual(qq_item.uid, netease_item.uid)
        self.client.release_qq.set()
        self.wait_until(lambda: not self.window.jobs)
        self.assertIn(netease.address, self.window.meta.text())
        self.assertIn("设计评审", self.window.body.toPlainText())
        self.assertFalse(self.service.list_messages(netease).items[0].unread)
        self.assertTrue(self.service.list_messages(qq).items[0].unread)

        self.show_mailbox("all")
        self.wait_until(lambda: self.window.mail_list.count() == 6)
        self.window.mail_list.setCurrentRow(0)
        self.wait_until(lambda: "阅览室" in self.window.body.toPlainText())
        self.assertIn(qq.address, self.window.meta.text())

    def test_combined_inbox_can_still_read_when_another_account_fails(self):
        qq, netease = ACCOUNTS
        self.bind_in_ui(qq)
        self.bind_in_ui(netease)
        self.client.unavailable.add(qq.address)
        self.show_mailbox("all")
        self.window.refresh_button.click()
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.window.mail_list.count(), 6)
        self.assertIn(qq.address, self.window.notice_label.text())
        self.assertIn("同步未完成", self.window.status.text())
        self.show_mailbox("account", netease.address)
        self.assertFalse(self.window.notice.isVisible())
        self.window.mail_list.setCurrentRow(0)
        self.wait_until(lambda: "设计评审" in self.window.body.toPlainText())
        self.show_mailbox("all")
        self.client.unavailable.clear()
        self.window.retry_button.click()
        self.wait_until(lambda: not self.window.notice.isVisible() and not self.window.jobs)
        self.assertEqual(self.window.mail_list.count(), 6)

    def test_account_and_smart_mailbox_navigation_reuses_loaded_pages(self):
        qq, netease = ACCOUNTS
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        with patch.object(self.client, "list_messages", wraps=self.client.list_messages) as fetch:
            for kind, account, count in (("all", None, 6), ("account", qq, 3), ("drafts", None, 0),
                                         ("account", netease, 3), ("flagged", None, 1), ("all", None, 6)):
                self.show_mailbox(kind, account.address if account else None)
                self.assertEqual(self.window.mail_list.count(), count)
            self.wait_until(lambda: not self.window.jobs)
            fetch.assert_not_called()
            self.window.refresh_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(fetch.call_count, 2)

    def test_warm_start_displays_saved_rows_during_refresh_and_replaces_old_generation(self):
        qq, netease = ACCOUNTS
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        self.window.close()
        self.window.deleteLater()
        fresh_item = replace(self.service.list_messages(qq).items[0], validity="2", subject="新收件箱")
        release = threading.Event()
        def fetch(account, secret, cursor):
            if not release.wait(4):
                raise TimeoutError("模拟同步超时")
            if account == qq:
                return MailPage([fresh_item], None)
            raise ConnectionError("离线")

        with patch.object(self.client, "list_messages", side_effect=fetch) as request:
            self.window = MainWindow(self.service)
            self.window.show()
            try:
                self.assertEqual(self.window.mail_list.count(), 6)
                self.show_mailbox("account", netease.address)
                self.assertEqual(self.window.mail_list.count(), 3)
            finally:
                release.set()
                self.client.release_qq.set()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(self.window.mail_list.count(), 3)
            self.assertIn("同步未完成", self.window.status.text())
            self.show_mailbox("account", qq.address)
            self.assertFalse(self.window.notice.isVisible())
            self.assertEqual(self.window.mail_list.count(), 1)
            shown = self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole)
            self.assertEqual((shown.account, shown.validity, shown.subject), (qq.address, "2", "新收件箱"))
            self.assertEqual(request.call_count, 2)

    def test_date_order_pagination_and_refresh_returns_to_newest(self):
        account = ACCOUNTS[0]
        self.bind_in_ui(account)
        template = self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole)
        uids = tuple(range(1, PAGE_SIZE + 2))  # Low UIDs have the newest dates.
        items = [replace(template, uid=uid, timestamp=template.timestamp - uid * 86400) for uid in uids]
        cursor = InboxCursor(account.address, template.validity, uids, PAGE_SIZE)
        first = MailPage(items[:PAGE_SIZE], items[PAGE_SIZE - 1].uid, cursor)
        last = MailPage(items[PAGE_SIZE:], None)
        with patch.object(self.client, "list_messages", side_effect=[first, last, first]) as fetch:
            self.window.refresh_button.click()
            self.wait_until(lambda: self.window.mail_list.count() == PAGE_SIZE)
            bar = self.window.mail_list.verticalScrollBar()
            self.wait_until(lambda: bar.maximum() > 0)
            bar.setValue(bar.maximum())
            self.window.older_button.click()
            self.wait_until(lambda: self.window.mail_list.count() == len(items))
            self.assertEqual(fetch.call_args.args[-1], cursor)
            self.assertFalse(self.window.older_button.isVisible())
            self.assertGreater(bar.value(), 0)
            self.assertEqual([self.window.mail_list.item(i).data(Qt.ItemDataRole.UserRole).uid
                              for i in range(len(items))], list(uids))
            self.window.refresh_button.click()
            self.wait_until(lambda: self.window.mail_list.count() == PAGE_SIZE)
            self.assertEqual(bar.value(), 0)
            self.assertEqual(self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole).uid, 1)

    def test_search_and_mailbox_filters_keep_reader_in_sync(self):
        self.client.release_qq.set()
        for account in ACCOUNTS:
            self.bind_in_ui(account)
        self.show_mailbox("flagged")
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.window.mail_list.count(), 1)
        self.window.next_button.click()
        self.wait_until(lambda: "评审" in self.window.body.toPlainText())
        self.wait_until(lambda: not self.window.jobs)
        self.window.search.setText("没有这封信")
        self.assertEqual(self.window.mail_list.count(), 0)
        self.assertEqual(self.window.body.toPlainText(), "")
        self.assertFalse(self.window.copy_button.isEnabled())
        self.window.search.clear()
        self.show_mailbox("unread")
        self.assertEqual(self.window.mail_list.count(), 3)
        self.window.search.setText("阅览室")
        self.assertEqual(self.window.mail_list.count(), 1)
        self.assertIn("阅览室", self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole).preview)
        self.window._toggle_sidebar()
        self.assertTrue(self.window.sidebar.isHidden())
        self.window.show_sidebar.click()
        self.assertFalse(self.window.sidebar.isHidden())

    def test_unread_click_updates_count_and_keeps_body_after_row_is_removed(self):
        from maildesk.ui import NAV_COUNT

        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        self.show_mailbox("unread")
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.window.account_list.currentItem().data(NAV_COUNT), 2)
        original = self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole)
        with patch.object(self.client, "mark_read", wraps=self.client.mark_read) as mark:
            self.click_message(0)
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(self.window.subject.text(), original.subject)
            self.assertIn("评审", self.window.body.toPlainText())
            self.assertTrue(self.window.copy_button.isEnabled())
            self.assertEqual(self.window.mail_list.count(), 1)
            self.assertEqual(self.window.account_list.currentItem().data(NAV_COUNT), 1)
            self.assertNotEqual(self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole).uid, original.uid)
            self.show_mailbox("account", account.address)
            self.window.refresh_button.click()
            self.wait_until(lambda: not self.window.jobs)
            refreshed = self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole)
            self.assertEqual(refreshed.uid, original.uid)
            self.assertFalse(refreshed.unread)
            self.click_message(0)
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(mark.call_count, 1)

    def test_failed_body_and_failed_mark_preserve_unread_until_successful_retry(self):
        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        self.show_mailbox("unread")
        self.wait_until(lambda: not self.window.jobs)
        with patch.object(self.client, "mark_read", side_effect=ConnectionError("无法保存已读")) as mark:
            with patch.object(self.client, "read_body", side_effect=ConnectionError("网络中断")):
                self.click_message(0)
                self.wait_until(lambda: not self.window.jobs)
                self.assertTrue(self.window.body_retry.isVisible())
                mark.assert_not_called()
            self.window.body_retry.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertIn("评审", self.window.body.toPlainText())
            self.assertTrue(self.window.mail_list.currentItem().data(Qt.ItemDataRole.UserRole).unread)
            self.assertTrue(self.window.reader.seen_retry.isVisible())
            self.assertTrue(self.window.reader.seen_status.text())
            mark.assert_called_once()
        with patch.object(self.client, "read_body", wraps=self.client.read_body) as fetch:
            self.window.reader.seen_retry.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(self.window.mail_list.count(), 1)
            self.assertIn("评审", self.window.body.toPlainText())
            self.assertFalse(self.window.reader.seen_retry.isVisible())
            fetch.assert_not_called()

    def test_late_mark_success_only_updates_original_account(self):
        qq, netease = ACCOUNTS
        self.client.release_qq.set()
        for account in (qq, netease):
            self.bind_in_ui(account)
        self.show_mailbox("account", qq.address)
        self.wait_until(lambda: not self.window.jobs)
        started, release = threading.Event(), threading.Event()
        original_mark = self.client.mark_read

        def delayed_mark(account, secret, item):
            started.set()
            if not release.wait(4):
                raise TimeoutError("等待切换窗口超时")
            original_mark(account, secret, item)

        with patch.object(self.client, "mark_read", side_effect=delayed_mark):
            self.click_message(0)
            try:
                self.wait_until(started.is_set)
                self.show_mailbox("account", netease.address)
                self.wait_until(lambda: self.window.mail_list.count() == 3)
            finally:
                release.set()
            self.wait_until(lambda: not self.window.jobs)
        shown = self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(shown.account, netease.address)
        self.assertTrue(shown.unread)
        self.assertEqual(self.window.body.toPlainText(), "")
        self.assertFalse(self.service.list_messages(qq).items[0].unread)

    def test_startup_does_not_read_or_mark_mail_before_user_opens_it(self):
        account = ACCOUNTS[1]
        self.service.bind(account.provider, account.address, "fixture-code")
        self.window.close()
        self.window.deleteLater()
        with patch.object(self.client, "read_content", wraps=self.client.read_content) as read:
            self.window = MainWindow(self.service)
            self.window.show()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(self.window.body.toPlainText(), "")
            read.assert_not_called()
        self.assertTrue(self.service.list_messages(account).items[0].unread)
        self.click_message(0)
        self.wait_until(lambda: not self.window.jobs)
        self.assertFalse(self.service.list_messages(account).items[0].unread)

    def test_refresh_started_while_marking_cannot_restore_stale_unread_dot(self):
        account = ACCOUNTS[1]
        self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        mark_started, finish_mark = threading.Event(), threading.Event()
        page_snapshot, return_page = threading.Event(), threading.Event()
        original_mark = self.client.mark_read
        original_list = self.client.list_messages

        def mark(*args):
            mark_started.set()
            if not finish_mark.wait(4):
                raise TimeoutError("标记超时")
            original_mark(*args)

        def fetch(*args):
            page = original_list(*args)
            page_snapshot.set()
            if not return_page.wait(4):
                raise TimeoutError("刷新超时")
            return page

        with patch.object(self.client, "mark_read", side_effect=mark), patch.object(
                self.client, "list_messages", side_effect=fetch):
            try:
                self.click_message(0)
                self.wait_until(mark_started.is_set)
                self.window.refresh_button.click()
                self.wait_until(page_snapshot.is_set)
                finish_mark.set()
                self.wait_until(lambda: len(self.window.jobs) == 1)
            finally:
                finish_mark.set()
                return_page.set()
            self.wait_until(lambda: not self.window.jobs)
        self.assertFalse(self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole).unread)

    def test_poll_both_accounts_open_popup_read_and_complete(self):
        qq, netease = ACCOUNTS
        self.client.release_qq.set()
        for account in (qq, netease):
            self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.service.list_pending(), [])
        self.assertFalse(self.window.popup.isVisible())

        self.show_mailbox("account", qq.address)
        self.client.simulate_arrival(netease)
        self.window.search.setText("先前的搜索")
        self.window.reminder_timer.setSingleShot(True)
        self.window.reminder_timer.start(20)
        self.wait_until(self.window.popup.isVisible)
        self.window.popup.findChild(QPushButton, "viewReminder").click()
        self.wait_until(lambda: self.window.copy_button.isEnabled())
        self.assertEqual(self.window.nav_kind, "pending")
        self.assertIn(netease.address, self.window.meta.text())
        self.assertEqual(self.window.mail_list.count(), 1)
        self.assertTrue(self.window.reader.complete.isVisible())
        self.window.reader.complete.click()
        self.wait_until(lambda: self.window.mail_list.count() == 0)
        self.assertEqual(self.window.body.toPlainText(), "")
        self.window.refresh_button.click()
        self.wait_until(lambda: not self.window.jobs)
        self.assertEqual(self.service.list_pending(), [])
        self.assertFalse(self.window.popup.isVisible())

    def test_reminders_continue_and_failed_account_can_retry(self):
        qq, netease = ACCOUNTS
        for account in (qq, netease):
            self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        self.client.unavailable.add(qq.address)
        self.client.simulate_arrival(qq)
        self.client.simulate_arrival(netease)
        self.window.reminder_timer.timeout.emit()
        self.wait_until(lambda: not self.window.jobs)
        self.show_mailbox("pending")
        self.assertEqual(self.window.mail_list.count(), 1)
        self.assertIn(qq.address, self.window.notice_label.text())
        self.assertEqual(self.window.mail_list.item(0).data(Qt.ItemDataRole.UserRole).account, netease.address)
        self.client.unavailable.clear()
        self.window.retry_button.click()
        self.wait_until(lambda: not self.window.jobs)
        self.assertFalse(self.window.notice.isVisible())
        self.assertEqual(self.window.mail_list.count(), 2)


class ProviderDesktopTests(DesktopTestCase):
    def test_startup_shows_cached_mail_and_consent_names_only_one_mailbox(self):
        qq, netease = ACCOUNTS
        for account in ACCOUNTS:
            self.service.bind(account.provider, account.address, "fixture")
            self.service.list_messages(account)
        trusted = set()
        prompts = []
        release = threading.Event()

        def passive(address):
            if address not in trusted:
                raise MailboxAuthorizationRequired()
            return "fixture"

        def authorize(account):
            prompts.append(account.address)
            if len(prompts) == 1:
                self.assertTrue(release.wait(4))
                raise CredentialAccessPaused()
            trusted.add(account.address)

        with patch.object(self.service._vault, "get_for_mail", side_effect=passive, create=True), \
                patch.object(self.service, "authorize_mailbox", side_effect=authorize):
            self.window._load_accounts()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(self.window.mail_list.count(), 6)
            self.assertEqual(self.window.retry_button.text(), "授权并记住")
            self.assertIn(qq.address, self.window.notice_label.text())
            self.assertIn("系统钥匙串", self.window.notice_label.text())
            self.show_mailbox("account", netease.address)
            self.window.refresh_button.click()
            self.window.reminder_timer.timeout.emit()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(prompts, [])
            self.assertIn(netease.address, self.window.retry_button.toolTip())
            self.window.retry_button.click()
            try:
                self.wait_until(lambda: bool(prompts))
                self.assertFalse(self.window.retry_button.isEnabled())
                self.show_mailbox("all")
                self.assertFalse(self.window.retry_button.isEnabled())
            finally:
                release.set()
            self.wait_until(lambda: not self.window.jobs)
            self.window.reminder_timer.timeout.emit()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(prompts, [netease.address])
            self.show_mailbox("account", netease.address)
            self.window.retry_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertFalse(self.window.notice.isVisible())
            self.show_mailbox("all")
            self.assertIn(qq.address, self.window.notice_label.text())
            self.show_mailbox("pending")
            self.assertIn(qq.address, self.window.retry_button.toolTip())
            self.assertEqual(prompts, [netease.address, netease.address])

    def test_dismiss_binding_while_waiting_for_the_credential_store(self):
        account = ACCOUNTS[0]
        started, release = threading.Event(), threading.Event()
        store = self.service._vault.set

        def delayed_store(address, secret):
            started.set()
            self.assertTrue(release.wait(4))
            return store(address, secret)

        with patch.object(self.service._vault, "set", side_effect=delayed_store):
            self.window.findChild(QPushButton, "bindButton").click()
            dialog = self.window.binding
            dialog.address.setText(account.address)
            dialog.secret.setText("fixture-key")
            dialog.submit.click()
            try:
                self.wait_until(started.is_set)
                self.assertTrue(dialog.close())
                self.assertFalse(dialog.isVisible())
                self.assertFalse(dialog.secret.text())
                self.assertIsNone(self.app.activeModalWidget())
            finally:
                release.set()
            self.wait_until(lambda: self.window.binding is None and not self.window.jobs)
            self.assertEqual(self.service.accounts(), [account])
            self.assertEqual(self.window.mail_list.count(), 3)

    def test_close_window_during_model_key_save_preserves_selection(self):
        started, release = threading.Event(), threading.Event()

        def delayed_store(address, secret):
            started.set()
            self.assertTrue(release.wait(4))

        with patch("maildesk.reply_settings.KeyringVault") as vault:
            vault.return_value.set.side_effect = delayed_store
            self.window.ai_settings_button.click()
            dialog = self.window.model_settings
            dialog.enabled.setChecked(True)
            dialog.key.setText("fixture-key")
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
            try:
                self.wait_until(started.is_set)
                dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(self.window.close())
                self.assertIsNone(self.app.activeModalWidget())
            finally:
                release.set()
            self.wait_until(lambda: not self.window.jobs)
        settings = QSettings(self.window.reply_prefs.fileName(), QSettings.Format.IniFormat)
        self.assertEqual(settings.value("selection")["provider"], "deepseek")
        self.assertTrue(settings.value("keys/deepseek", False, type=bool))

    def test_only_explicit_retry_or_refresh_resumes_credential_prompts(self):
        account = ACCOUNTS[0]
        self.bind_in_ui(account)
        self.wait_until(lambda: not self.window.jobs)
        self.client.unavailable.add(account.address)
        with patch("maildesk.ui.KeyringVault.resume_access") as resume:
            self.window.reminder_timer.timeout.emit()
            self.show_mailbox("account", account.address)
            self.wait_until(lambda: not self.window.jobs)
            resume.assert_not_called()
            self.window.refresh_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(resume.call_count, 1)
            self.window.retry_button.click()
            self.wait_until(lambda: not self.window.jobs)
            self.assertEqual(resume.call_count, 2)

    def test_suffix_selection_preserves_local_name_and_shows_provider_instructions(self):
        self.window.findChild(QPushButton, "bindButton").click()
        dialog = self.window.binding
        self.assertEqual({dialog.domain.itemText(i) for i in range(dialog.domain.count())},
                         {"@" + domain for profile in MAIL_PROVIDERS.values() for domain in profile.domains})
        QTest.keyClicks(dialog.address, "reader")
        for code, profile in MAIL_PROVIDERS.items():
            for domain in profile.domains:
                with self.subTest(domain=domain):
                    dialog.domain.setCurrentText("@" + domain)
                    self.assertEqual(dialog.domain.currentData(), code)
                    self.assertEqual(dialog.address.text(), "reader")
                    self.assertEqual(dialog.credential_label.text(), profile.credential_label)
                    self.assertIn(profile.credential_label, dialog.secret.placeholderText())
                    self.assertEqual(dialog.setup_hint.text(), profile.setup_hint)
                    self.assertIn(profile.help_url, dialog.help_link.text())
                    self.assertTrue(dialog.help_link.isVisible())
                    self.assertTrue(dialog.help_link.openExternalLinks())
        dialog.secret.setText("previous-provider-code")
        dialog.domain.setCurrentText("@gmail.com")
        self.assertEqual(dialog.secret.text(), "")
        dialog.cancel.click()

    def test_complete_address_is_split_and_unknown_pasted_domain_never_reaches_login(self):
        self.window.findChild(QPushButton, "bindButton").click()
        dialog = self.window.binding
        dialog.address.setFocus()
        self.wait_until(dialog.address.hasFocus)
        QTest.keyClicks(dialog.address, "reader@GOOGLEMAIL.COM")
        QTest.keyClick(dialog.address, Qt.Key.Key_Tab)
        self.assertEqual(dialog.address.text(), "reader")
        self.assertEqual(dialog.domain.currentText(), "@googlemail.com")
        self.assertEqual(dialog.domain.currentData(), "gmail")
        dialog.address.selectAll()
        QTest.keyClicks(dialog.address, "reader@gmail.com.example.org")
        dialog.secret.setText("aaaa bbbb cccc dddd")
        with patch.object(self.client, "verify") as verify:
            dialog.submit.click()
            self.assertIn("后缀", dialog.status.text())
            verify.assert_not_called()
        dialog.cancel.click()

    def test_switching_between_gmail_and_163_reads_and_updates_the_selected_account(self):
        gmail = Account("reader@gmail.com", "gmail")
        netease = ACCOUNTS[1]
        with patch.dict(self.client._examples, gmail=self.client._examples["163"]):
            self.bind_in_ui(gmail, "aaaa bbbb cccc dddd")
            self.bind_in_ui(netease)
            for account in (gmail, netease, gmail):
                self.show_mailbox("account", account.address)
                self.wait_until(lambda: self.window.mail_list.count() == 3 and not self.window.jobs)
                self.assertEqual(self.window.heading.text(), "收件箱 — " + MAIL_PROVIDERS[account.provider].name)
                self.click_message(0)
                self.wait_until(lambda: self.window.body_ready and not self.window.jobs)
                self.assertEqual(self.window.reading_item.account, account.address)
            self.window.update_button.click()
            dialog = self.window.binding
            self.assertTrue(dialog.address.isReadOnly())
            self.assertFalse(dialog.domain.isEnabled())
            self.assertEqual(dialog.address.text(), "reader")
            self.assertEqual(dialog.domain.currentText(), "@gmail.com")
            dialog.secret.setText("aaaa 1111 bbbb 2222")
            with patch.object(self.client, "verify", wraps=self.client.verify) as verify:
                dialog.submit.click()
                self.wait_until(lambda: self.window.binding is None and not self.window.jobs)
                verify.assert_called_once_with(gmail, "aaaa1111bbbb2222")
            self.assertEqual(self.service.accounts(), [gmail, netease])
