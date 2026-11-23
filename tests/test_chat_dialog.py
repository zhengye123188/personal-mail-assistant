import os
import unittest
from concurrent.futures import CancelledError
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from maildesk.chat_dialog import ChatDialog
from maildesk.mail_chat import ChatTurn
from maildesk.models import Account, MailItem


class ChatDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.qq = Account("me@qq.com", "qq")
        self.netease = Account("me@163.com", "163")
        self.accounts = (self.qq, self.netease)
        self.item = MailItem(self.qq.address, "42", 1, "<b>周五 & 读书会</b>", "王琳",
                             self.qq.address, "2026-11-23", 123)
        self.turn = ChatTurn(question="查询活动", answer="读书会定在周五。[1]",
                                    sources=(self.item,), draft_item=None, draft_text="")
        self.ask = Mock(return_value=self.turn)
        self.jobs = []
        self.ui = ChatDialog(accounts=self.accounts, model_description="DeepSeek 云端", ask=self.ask,
                             run=lambda work, done: self.jobs.append((work, done)))
        self.ui.show()
        self.addCleanup(self.ui.deleteLater)
        self.addCleanup(self.ui.close)

    def finish(self):
        work, done = self.jobs.pop(0)
        try:
            result = work()
        except Exception as exc:
            done(None, exc)
        else:
            done(result, None)

    def test_sources_and_followup_handoff_require_clicks(self):
        d = self.ui
        self.ask.assert_not_called()
        self.assertFalse(self.jobs)
        self.assertIn("DeepSeek", d.model_label.text())
        opened, composed = [], []
        d.open_mail.connect(opened.append)
        d.compose_reply.connect(lambda item, text: composed.append((item, text)))
        d.input.setPlainText(self.turn.question)
        QTest.keyClick(d.input, Qt.Key.Key_Return)
        self.assertEqual(d.send_button.text(), "停止")
        self.finish()
        self.assertEqual(self.ask.call_args.args, (self.turn.question, self.accounts, ()))
        self.assertEqual(d.history, [self.turn])
        self.assertFalse(opened)
        source = d.findChild(QPushButton, "chatSource")
        self.assertIn("[1]", source.accessibleName())
        source.click()
        self.assertEqual(opened, [self.item])

        followup = ChatTurn(question="帮我回复第一封", answer="草稿已准备好。",
                                   sources=(self.item,), draft_item=self.item, draft_text="谢谢邀请，周五见。")
        self.ask.return_value = followup
        d.input.setPlainText(followup.question)
        d.send_button.click()
        self.finish()
        self.assertEqual(self.ask.call_args.args[2], (self.turn,))
        self.assertEqual(d.findChild(QLabel, "chatDraftText").text(), followup.draft_text)
        self.assertFalse(composed)
        d.findChild(QPushButton, "chatCompose").click()
        self.assertEqual(composed, [(self.item, followup.draft_text)])

    def test_switching_scope_model_and_starting_new_chat_drop_late_results(self):
        d = self.ui
        d.suggestions[0].click()
        work, done = self.jobs.pop()
        turn = work()
        token = self.ask.call_args.kwargs["cancelled"]
        generation = d.conversation_id
        d.scope.setCurrentIndex(d.scope.findData(self.netease.address))
        self.assertGreater(d.conversation_id, generation)
        self.assertTrue(token())
        done(turn, None)
        self.assertFalse(d.history)
        self.assertFalse(d.input.toPlainText())
        d.suggestions[1].click()
        self.finish()
        self.assertEqual(self.ask.call_args.args[1:3], ((self.netease,), ()))
        d.new_chat.click()
        self.assertFalse(d.history)

        d.suggestions[1].click()
        queued, old_done = self.jobs.pop()
        new_model = Mock(return_value=self.turn)
        d.reconfigure(accounts=self.accounts, model_description="通义千问", ask=new_model)
        with self.assertRaises(CancelledError):
            queued()
        old_done(None, CancelledError())
        self.assertFalse(d.history)
        self.assertIn("通义千问", d.model_label.text())
        d.suggestions[0].click()
        self.finish()
        new_model.assert_called_once()
        d.disable()
        self.assertFalse(d.history)
        self.assertFalse(d.isVisible())

    def test_stop_close_and_retry_keep_completed_history_and_question(self):
        d = self.ui
        d.suggestions[0].click()
        self.finish()
        d.input.setPlainText("再看第二封")
        d.send_button.click()
        work, done = self.jobs.pop()
        generation = d.conversation_id
        d.close()
        self.assertGreater(d.conversation_id, generation)
        with self.assertRaises(CancelledError):
            work()
        done(self.turn, None)
        d.show()
        self.assertEqual(d.history, [self.turn])
        self.assertEqual(d.input.toPlainText(), "再看第二封")
        self.ask.side_effect = RuntimeError("模型暂时不可用")
        d.send_button.click()
        self.finish()
        self.assertIn("模型暂时不可用", d.status.text())
        self.assertEqual(d.history, [self.turn])
        self.assertEqual(d.input.toPlainText(), "再看第二封")
        self.ask.side_effect = None
        d.send_button.click()
        d.send_button.click()
        self.finish()
        self.assertEqual(d.history, [self.turn])
        self.assertEqual(d.input.toPlainText(), "再看第二封")

    def test_deleting_dialog_cancels_queued_work_without_accessing_widgets(self):
        d = ChatDialog(accounts=self.accounts, model_description="Demo", ask=self.ask,
                       run=lambda work, done: self.jobs.append((work, done)))
        d.suggestions[0].click()
        d.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        work, done = self.jobs.pop()
        with self.assertRaises(CancelledError):
            work()
        done(self.turn, None)
        self.ask.assert_not_called()
