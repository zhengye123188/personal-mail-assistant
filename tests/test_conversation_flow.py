import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from maildesk.agent_store import AgentStore, HISTORY_LIMIT
from maildesk.chat_dialog import ChatDialog
from maildesk.mail_chat import ChatTurn
from maildesk.models import Account, MailItem


class ConversationFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "agent.sqlite3"
        self.store = AgentStore(self.path)
        self.qq = Account("personal@qq.com", "qq")
        self.work = Account("work@163.com", "163")
        self.jobs = []
        self.ask = Mock(side_effect=self.answer)
        self.ui = self.open_dialog()

    def answer(self, question, accounts, history, *, cancelled):
        address = accounts[0].address
        mail = MailItem(address, "17", 41, question, "编辑部", address, "2026-11-24", 0)
        return ChatTurn(question, "模拟答复 " + question, (mail,), scope=(address,))

    def open_dialog(self):
        dialog = ChatDialog(
            accounts=(self.qq, self.work), model_description="测试模型", ask=self.ask,
            store=AgentStore(self.path), run=lambda work, done: self.jobs.append((work, done)),
        )
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.show()
        dialog.scope.setCurrentIndex(dialog.scope.findData(self.qq.address))
        self.app.processEvents()
        return dialog

    def begin(self, question):
        self.ui.input.setPlainText(question)
        self.ui.send_button.click()
        self.assertEqual(len(self.jobs), 1)
        return self.jobs.pop()

    def send(self, question):
        work, done = self.begin(question)
        done(work(), None)
        self.assertEqual(self.ui.history[-1].question, question)
        return self.ui.conversation_bar.selector.currentData()

    def select(self, conversation_id):
        selector = self.ui.conversation_bar.selector
        row = selector.findData(conversation_id)
        self.assertGreaterEqual(row, 0)
        selector.showPopup()
        self.app.processEvents()
        view = selector.view()
        index = selector.model().index(row, selector.modelColumn())
        view.scrollTo(index)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(index).center())
        self.app.processEvents()
        self.assertEqual(selector.currentData(), conversation_id)

    def choose_delete(self, role):
        self.ui.conversation_bar.delete_button.click()
        self.app.processEvents()
        prompt, = [widget for widget in self.ui.findChildren(QMessageBox, "deleteConversationPrompt")
                   if widget.isVisible()]
        button, = [button for button in prompt.buttons() if prompt.buttonRole(button) == role]
        button.click()
        self.app.processEvents()

    def questions(self, account, conversation_id):
        return [turn.question for turn in self.store.history((account,), conversation_id=conversation_id)]

    def displayed_questions(self):
        questions = []
        for i in range(self.ui.messages.count()):
            row = self.ui.messages.itemAt(i).widget()
            questions.extend(label.text() for label in row.findChildren(QLabel, "chatQuestion"))
        return questions

    def test_switch_follow_up_and_reopen_only_use_selected_thread(self):
        first = self.send("查投稿截止时间")
        self.assertIsInstance(first, int)
        self.assertEqual(self.send("需要哪些附件？"), first)
        self.ui.input.setPlainText("尚未发出的投稿问题")
        self.ui.conversation_bar.new_button.click()
        self.assertFalse(self.ui.history)
        self.assertFalse(self.ui.input.toPlainText())
        self.assertFalse(self.ui.conversation_bar.delete_button.isEnabled())
        second = self.send("查会议发票")
        self.assertNotEqual(first, second)
        self.assertEqual(self.ask.call_args.args[2], ())
        self.select(first)
        self.assertEqual([t.question for t in self.ui.history], ["查投稿截止时间", "需要哪些附件？"])
        self.assertEqual(self.ui.input.toPlainText(), "尚未发出的投稿问题")
        self.send("把这封信总结一下")
        self.assertEqual([t.question for t in self.ask.call_args.args[2]],
                         ["查投稿截止时间", "需要哪些附件？"])
        self.ui.scope.setCurrentIndex(self.ui.scope.findData(self.work.address))
        self.assertEqual(self.ui.conversation_bar.selector.findData(first), -1)
        self.assertEqual(self.ui.conversation_bar.selector.findData(second), -1)
        self.assertFalse(self.ui.history)
        third = self.send("查本周售后记录")
        self.assertEqual(self.ask.call_args.args[2], ())
        self.ui.close()

        self.ui = self.open_dialog()
        self.assertEqual(self.ui.conversation_bar.selector.findData(third), -1)
        self.select(second)
        self.assertEqual([t.question for t in self.ui.history], ["查会议发票"])
        self.assertEqual(self.ui.history[0].sources[0].account, self.qq.address)
        self.select(first)
        self.assertEqual(len(self.ui.history), 3)

    def test_late_results_after_switch_and_new_chat_cannot_cross_threads(self):
        first = self.send("第一组邮件")
        self.ui.conversation_bar.new_button.click()
        second = self.send("第二组邮件")
        work, done = self.begin("较迟到达的答复")
        late = work()
        cancelled = self.ask.call_args.kwargs["cancelled"]
        self.select(first)
        self.assertTrue(cancelled())
        done(late, None)
        self.assertEqual([turn.question for turn in self.ui.history], ["第一组邮件"])
        self.assertEqual(self.questions(self.qq, second), ["第二组邮件"])

        work, done = self.begin("新对话打开后才到达")
        late = work()
        self.ui.conversation_bar.new_button.click()
        done(late, None)
        self.assertFalse(self.ui.history)
        self.assertEqual(self.questions(self.qq, first), ["第一组邮件"])
        self.assertEqual(len(self.store.list_conversations((self.qq,))), 2)

    def test_cancel_then_delete_only_current_conversation_and_keep_its_task(self):
        first = self.send("项目验收")
        source = self.ui.history[0].sources[0]
        self.store.add_task("提交报告", datetime(2026, 12, 1, tzinfo=timezone.utc), source)
        self.ui.conversation_bar.new_button.click()
        second = self.send("例会安排")
        self.select(first)
        self.choose_delete(QMessageBox.ButtonRole.RejectRole)
        self.assertEqual([turn.question for turn in self.ui.history], ["项目验收"])
        self.assertEqual(self.questions(self.qq, first), ["项目验收"])
        self.choose_delete(QMessageBox.ButtonRole.DestructiveRole)
        self.assertEqual([c.id for c in self.store.list_conversations((self.qq,))], [second])
        self.assertEqual(self.ui.conversation_bar.selector.findData(first), -1)
        self.assertEqual(self.questions(self.qq, second), ["例会安排"])
        self.assertEqual([task.title for task in self.store.list_tasks((self.qq,))], ["提交报告"])
        self.ui.close()

        self.ui = self.open_dialog()
        self.assertEqual(self.ui.conversation_bar.selector.findData(first), -1)
        self.select(second)
        self.choose_delete(QMessageBox.ButtonRole.DestructiveRole)
        self.assertFalse(self.ui.history)
        self.assertFalse(self.ui.conversation_bar.delete_button.isEnabled())
        self.assertFalse(self.store.list_conversations((self.qq,)))

    def test_read_earlier_page_and_ask_again_uses_latest_context(self):
        self.send("其他会话的私密问题")
        self.ui.conversation_bar.new_button.click()
        questions = [f"询问第 {n} 项" for n in range(HISTORY_LIMIT + 3)]
        for question in questions:
            thread = self.send(question)
        self.ui.close()
        self.ui = self.open_dialog()
        self.select(thread)
        self.assertEqual(self.displayed_questions(), questions[-HISTORY_LIMIT:])
        self.ui.older_turns.click()
        self.assertEqual(self.displayed_questions(), questions[:-HISTORY_LIMIT])
        self.assertEqual([t.question for t in self.ui.history], questions[-HISTORY_LIMIT:])
        self.ui.newer_turns.click()
        self.assertEqual(self.displayed_questions(), questions[-HISTORY_LIMIT:])
        self.ui.older_turns.click()
        self.assertEqual(self.send("接着刚才的问题"), thread)
        self.assertEqual([t.question for t in self.ask.call_args.args[2]], questions[-HISTORY_LIMIT:])
        self.assertEqual(self.displayed_questions(), questions[-(HISTORY_LIMIT - 1):] + ["接着刚才的问题"])

    def test_deleted_thread_is_not_recreated_by_late_reply(self):
        keep = self.send("保留这段对话")
        self.ui.conversation_bar.new_button.click()
        removed = self.send("即将删除的对话")
        work, done = self.begin("删除之前开始的提问")
        late = work()
        cancelled = self.ask.call_args.kwargs["cancelled"]
        self.choose_delete(QMessageBox.ButtonRole.DestructiveRole)
        self.assertTrue(cancelled())
        done(late, None)
        self.assertEqual(self.ui.conversation_bar.selector.findData(removed), -1)
        self.assertEqual([c.id for c in self.store.list_conversations((self.qq,))], [keep])
        self.assertEqual(self.questions(self.qq, keep), ["保留这段对话"])
        self.assertNotIn(late.question, self.displayed_questions())
