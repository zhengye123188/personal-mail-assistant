import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maildesk.agent_store import AgentStore, HISTORY_LIMIT
from maildesk.agent_widgets import DeadlineCard
from maildesk.chat_dialog import ChatDialog
from maildesk.mail_chat import ChatTurn, DeadlineProposal
from maildesk.models import Account, MailItem


class AgentDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = AgentStore(Path(temp.name) / "agent.sqlite3")
        self.qq = Account("testing@qq.com", "qq")
        self.other = Account("testing@163.com", "163")
        self.accounts = (self.qq, self.other)
        self.item = MailItem(self.qq.address, "11", 2, "周五材料", "张老师",
                             self.qq.address, "2026-11-23", 0)
        self.due = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
        self.proposal = DeadlineProposal(self.item, "提交材料", self.due, "请于11月27日18:00前提交材料。")
        self.ask = Mock(return_value=ChatTurn("找 deadline", "这封信里有一个截止日期。", (self.item,),
                                              deadline_proposals=(self.proposal,)))
        self.jobs = []

    def dialog(self, *, offline=False):
        ui = ChatDialog(accounts=self.accounts, model_description="DeepSeek" if not offline else "",
                        ask=None if offline else self.ask, store=self.store,
                        run=lambda work, done: self.jobs.append((work, done)))
        self.addCleanup(ui.deleteLater)
        self.addCleanup(ui.close)
        return ui

    def send(self, ui):
        ui._send("找 deadline")
        work, done = self.jobs.pop()
        done(work(), None)

    def test_evidence_confirm_edit_complete_and_source_without_model(self):
        d = self.dialog()
        d.scope.setCurrentIndex(d.scope.findData(self.qq.address))
        self.send(d)
        self.assertFalse(self.store.list_tasks(self.accounts))
        card = d.findChild(DeadlineCard)
        card.title_edit.setText("整理并提交材料")
        card.due_edit.setDateTime(card.due_edit.dateTime().addSecs(3600))
        changed = []
        d.tasks_changed.connect(lambda: changed.append(True))
        card.confirm.click()
        self.assertEqual(changed, [True])
        task, = self.store.list_tasks(self.accounts)
        self.assertEqual((task.title, task.due_at, task.evidence),
                         ("整理并提交材料", self.due + timedelta(hours=1), self.proposal.evidence))
        self.assertFalse(card.confirm.isEnabled())

        offline = self.dialog(offline=True)
        offline.show_tasks(self.other.address)
        self.assertEqual(offline.tasks.list.count(), 0)
        offline.show_tasks(self.qq.address)
        self.assertEqual(offline.tasks.list.count(), 1)
        opened = []
        offline.open_mail.connect(opened.append)
        offline.tasks.open_source.click()
        self.assertEqual(opened[0].uid, self.item.uid)
        offline.tasks.complete.click()
        self.assertEqual(offline.tasks.list.count(), 0)
        offline.tasks.show_completed.setChecked(True)
        self.assertEqual(offline.tasks.list.count(), 1)
        self.assertFalse(offline.tasks.complete.isEnabled())

    def test_preference_save_reopen_and_new_chat_preserves_previous_history(self):
        d = self.dialog()
        self.send(d)
        d.scope.setCurrentIndex(d.scope.findData(self.qq.address))
        self.assertFalse(d.history)
        self.send(d)
        d.prefs.language.setCurrentText("English")
        d.prefs.detail.setCurrentText("详细")
        d.prefs.lead.setValue(120)
        d.prefs.focus.setText("项目验收与交付")
        d.prefs.save.click()
        reopened = self.dialog()
        self.assertEqual(reopened.prefs.lead.value(), 120)
        self.assertEqual(reopened.prefs.focus.text(), "项目验收与交付")
        self.assertEqual(reopened.prefs.language.currentText(), "English")
        self.assertEqual(reopened.prefs.detail.currentText(), "详细")
        self.assertEqual([turn.question for turn in reopened.history], ["找 deadline"])
        reopened.scope.setCurrentIndex(reopened.scope.findData(self.other.address))
        self.assertFalse(reopened.history)
        reopened.scope.setCurrentIndex(reopened.scope.findData(self.qq.address))
        self.assertEqual(len(reopened.history), 1)
        self.assertFalse(reopened.findChildren(DeadlineCard))
        reopened.new_chat.click()
        self.assertFalse(reopened.history)
        self.assertEqual(len(self.store.history((self.qq,))), 1)
        self.assertEqual(len(self.store.history(self.accounts)), 1)

    def test_cancelled_reply_never_enters_memory_and_widget_count_is_bounded(self):
        d = self.dialog()
        d._send("找 deadline")
        work, done = self.jobs.pop()
        turn = work()
        d.scope.setCurrentIndex(d.scope.findData(self.qq.address))
        done(turn, None)
        self.assertFalse(self.store.history(self.accounts))
        self.assertFalse(d.history)
        for _ in range(HISTORY_LIMIT + 3):
            self.send(d)
        self.assertEqual(len(d.history), HISTORY_LIMIT)
        self.assertEqual(d.messages.count(), 2 * HISTORY_LIMIT)

    def test_unconfirmed_card_survives_switch_and_confirmed_card_stays_resolved(self):
        d = self.dialog()
        self.send(d)
        bar = d.conversation_bar
        thread = bar.selector.currentData()
        bar.new_button.click()
        bar.selector.setCurrentIndex(bar.selector.findData(thread))
        card, = d.messages.itemAt(1).widget().findChildren(DeadlineCard)
        card.confirm.click()
        bar.new_button.click()
        bar.selector.setCurrentIndex(bar.selector.findData(thread))
        self.assertFalse(d.messages.itemAt(1).widget().findChildren(DeadlineCard))
        self.assertEqual(len(self.store.list_tasks(self.accounts)), 1)

    def test_compact_navigation_and_empty_task_handoff(self):
        d = self.dialog()
        d.resize(390, 420)
        d.show()
        d.show_tasks(self.qq.address)
        self.app.processEvents()
        tabs = d.pages.tabBar()
        for index in range(tabs.count()):
            center = tabs.mapTo(d, tabs.tabRect(index).center())
            self.assertIs(d.childAt(center), tabs)
        d.tasks.empty_action.click()
        self.assertEqual(d.pages.currentIndex(), 0)
        self.assertIn("截止日期", d.input.toPlainText())
        self.assertEqual(d.scope.currentData(), self.qq.address)
        self.assertFalse(self.jobs)
        d.input.setPlainText("我正在写的问题")
        d.show_tasks()
        d.tasks.empty_action.click()
        self.assertEqual(d.input.toPlainText(), "我正在写的问题")
        d.pages.setCurrentWidget(d.prefs)
        self.assertFalse(d.scope.isVisible())
        d.show_tasks()
        self.assertTrue(d.scope.isVisible())
