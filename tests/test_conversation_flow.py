import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
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
        item = self.ui.conversation_sidebar.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole)

    def listed_ids(self):
        view = self.ui.conversation_sidebar.list
        return [view.item(i).data(Qt.ItemDataRole.UserRole) for i in range(view.count())]

    def click_row(self, conversation_id, *, trash=False):
        view = self.ui.conversation_sidebar.list
        self.assertTrue(view.isVisible())
        item = view.item(self.listed_ids().index(conversation_id))
        view.scrollToItem(item)
        self.app.processEvents()
        rect = view.visualItemRect(item)
        pos = QPoint(rect.right() - 16, rect.center().y()) if trash else rect.center()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.app.processEvents()

    def select(self, conversation_id):
        self.click_row(conversation_id)
        item = self.ui.conversation_sidebar.list.currentItem()
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole), conversation_id)

    def choose_delete(self, conversation_id, role):
        self.click_row(conversation_id, trash=True)
        prompt, = [widget for widget in self.ui.findChildren(QMessageBox, "deleteConversationPrompt")
                   if widget.isVisible()]
        thread, = [thread for thread in self.store.list_conversations((self.qq,))
                   if thread.id == conversation_id]
        self.assertIn(thread.title, prompt.text())
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
        self.ui.conversation_sidebar.new_button.click()
        self.assertFalse(self.ui.history)
        self.assertFalse(self.ui.input.toPlainText())
        self.assertFalse(self.ui.conversation_sidebar.list.selectedItems())
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
        self.assertNotIn(first, self.listed_ids())
        self.assertNotIn(second, self.listed_ids())
        self.assertFalse(self.ui.history)
        third = self.send("查本周售后记录")
        self.assertEqual(self.ask.call_args.args[2], ())
        self.ui.close()

        self.ui = self.open_dialog()
        self.assertNotIn(third, self.listed_ids())
        self.select(second)
        self.assertEqual([t.question for t in self.ui.history], ["查会议发票"])
        self.assertEqual(self.ui.history[0].sources[0].account, self.qq.address)
        self.select(first)
        self.assertEqual(len(self.ui.history), 3)

    def test_late_results_after_switch_and_new_chat_cannot_cross_threads(self):
        first = self.send("第一组邮件")
        self.ui.conversation_sidebar.new_button.click()
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
        self.ui.conversation_sidebar.new_button.click()
        done(late, None)
        self.assertFalse(self.ui.history)
        self.assertEqual(self.questions(self.qq, first), ["第一组邮件"])
        self.assertEqual(len(self.store.list_conversations((self.qq,))), 2)

    def test_cancel_then_delete_only_current_conversation_and_keep_its_task(self):
        first = self.send("项目验收")
        source = self.ui.history[0].sources[0]
        self.store.add_task("提交报告", datetime(2026, 12, 1, tzinfo=timezone.utc), source)
        self.ui.conversation_sidebar.new_button.click()
        second = self.send("例会安排")
        self.select(first)
        self.choose_delete(first, QMessageBox.ButtonRole.RejectRole)
        self.assertEqual([turn.question for turn in self.ui.history], ["项目验收"])
        self.assertEqual(self.questions(self.qq, first), ["项目验收"])
        self.choose_delete(first, QMessageBox.ButtonRole.DestructiveRole)
        self.assertEqual([c.id for c in self.store.list_conversations((self.qq,))], [second])
        self.assertNotIn(first, self.listed_ids())
        self.assertEqual([turn.question for turn in self.ui.history], ["例会安排"])
        self.assertEqual(self.questions(self.qq, second), ["例会安排"])
        self.assertEqual([task.title for task in self.store.list_tasks((self.qq,))], ["提交报告"])
        self.ui.close()

        self.ui = self.open_dialog()
        self.assertNotIn(first, self.listed_ids())
        self.select(second)
        self.choose_delete(second, QMessageBox.ButtonRole.DestructiveRole)
        self.assertFalse(self.ui.history)
        self.assertEqual(self.listed_ids(), [])
        self.assertFalse(self.store.list_conversations((self.qq,)))

    def test_read_earlier_page_and_ask_again_uses_latest_context(self):
        self.send("其他会话的私密问题")
        self.ui.conversation_sidebar.new_button.click()
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
        self.ui.conversation_sidebar.new_button.click()
        removed = self.send("即将删除的对话")
        work, done = self.begin("删除之前开始的提问")
        late = work()
        cancelled = self.ask.call_args.kwargs["cancelled"]
        self.choose_delete(removed, QMessageBox.ButtonRole.DestructiveRole)
        self.assertTrue(cancelled())
        done(late, None)
        self.assertNotIn(removed, self.listed_ids())
        self.assertEqual([c.id for c in self.store.list_conversations((self.qq,))], [keep])
        self.assertEqual(self.questions(self.qq, keep), ["保留这段对话"])
        self.assertNotIn(late.question, self.displayed_questions())

    def test_deleting_other_thread_keeps_current_request_and_unsent_text(self):
        other = self.send("旧的会议安排")
        self.ui.conversation_sidebar.new_button.click()
        current = self.send("正在讨论的报告")
        work, done = self.begin("继续整理报告")
        late = work()
        cancelled = self.ask.call_args.kwargs["cancelled"]
        self.ui.input.setPlainText("下一轮想问交付时间")

        for role in (QMessageBox.ButtonRole.RejectRole, QMessageBox.ButtonRole.DestructiveRole):
            self.choose_delete(other, role)
            self.assertFalse(cancelled())
            self.assertEqual(self.ui.input.toPlainText(), "下一轮想问交付时间")
            self.assertEqual(self.ui.conversation_sidebar.list.currentItem().data(Qt.ItemDataRole.UserRole),
                             current)
            self.assertEqual(self.displayed_questions(), ["正在讨论的报告", "继续整理报告"])

        done(late, None)
        self.assertEqual(self.questions(self.qq, current), ["正在讨论的报告", "继续整理报告"])
        self.assertEqual(self.listed_ids(), [current])
        self.assertEqual(self.ui.input.toPlainText(), "下一轮想问交付时间")

    def test_deleting_other_thread_preserves_current_history_page(self):
        old = self.send("可以删除")
        self.ui.conversation_sidebar.new_button.click()
        for n in range(HISTORY_LIMIT + 1):
            self.send(f"测试问题 {n}")
        self.ui.older_turns.click()
        self.assertEqual(self.displayed_questions(), ["测试问题 0"])
        self.choose_delete(old, QMessageBox.ButtonRole.DestructiveRole)
        self.assertEqual(self.displayed_questions(), ["测试问题 0"])
        self.assertTrue(self.ui.newer_turns.isEnabled())

    def test_compact_history_can_switch_return_and_start_new_then_dock_again(self):
        first = self.send("历史会议")
        self.ui.conversation_sidebar.new_button.click()
        self.send("新的会议")
        self.ui.resize(390, 420)
        self.app.processEvents()
        sidebar = self.ui.conversation_sidebar
        self.assertFalse(sidebar.isVisible())
        self.assertTrue(self.ui.history_button.isVisible())
        self.ui.history_button.click()
        self.assertTrue(sidebar.list.isVisible())
        self.assertFalse(self.ui.input.isVisible())
        self.select(first)
        self.assertTrue(self.ui.input.isVisible())
        self.assertFalse(sidebar.isVisible())
        self.assertEqual(self.displayed_questions(), ["历史会议"])
        self.ui.input.setPlainText("未发送的内容")
        self.ui.history_button.click()
        self.ui.history_back.click()
        self.assertEqual(self.ui.input.toPlainText(), "未发送的内容")
        self.ui.history_button.click()
        sidebar.new_button.click()
        self.assertTrue(self.ui.input.isVisible())
        self.assertFalse(self.ui.history)
        self.ui.resize(860, 660)
        self.app.processEvents()
        self.assertTrue(sidebar.list.isVisible())
        self.assertTrue(self.ui.input.isVisible())
        self.assertFalse(self.ui.history_button.isVisible())

    def test_compact_keyboard_can_browse_before_opening_a_conversation(self):
        for title in ("报销", "会议", "行程"):
            self.ui.new_chat.click()
            self.send(title)
        self.ui.resize(390, 420)
        self.app.processEvents()
        view = self.ui.conversation_sidebar.list
        self.ui.history_button.click()
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertTrue(view.isVisible())
        self.assertEqual(view.currentItem().text(), "报销")
        self.ui.history_back.click()
        self.assertEqual(self.displayed_questions(), ["行程"])
        self.ui.history_button.click()
        self.assertEqual(view.currentItem().text(), "行程")
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertTrue(self.ui.input.isVisible())
        self.assertEqual(self.displayed_questions(), ["会议"])
