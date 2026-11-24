import os
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from maildesk.agent_store import AgentStore
from maildesk.agent_tasks import TaskPage, _deadline_label
from maildesk.models import MailItem


class TaskPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.store = AgentStore(Path(folder) / "test.sqlite3")
        self.item = MailItem("sample@qq.com", "1", 3, "材料交付", "设计组", "sample@qq.com", "", 0)
        self.now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        self.page = TaskPage(self.store)
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(self.page.close)
        self.page.scope = (self.item.account,)

    def test_urgency_source_and_selection_survive_filter(self):
        overdue = self.store.add_task("提交材料", self.now - timedelta(hours=2), self.item, "请提交材料。")
        today = self.store.add_task("确认议程", self.now + timedelta(hours=2), self.item)
        tomorrow = self.store.add_task("确认场地", self.now + timedelta(days=1), self.item)
        self.assertEqual([_deadline_label(task, self.now)[0] for task in (overdue, today, tomorrow)],
                         ["已逾期", "今天", "明天"])
        p = self.page
        p.refresh()
        p.list.setCurrentRow(1)
        p.show_completed.setChecked(True)
        self.assertEqual(p.list.currentItem().data(Qt.ItemDataRole.UserRole), today)
        p.complete.click()
        selected = p.list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.assertTrue(selected.completed)
        self.assertEqual(selected.id, today.id)
        self.assertEqual(_deadline_label(selected, self.now)[0], "已完成")
        self.assertFalse(p.complete.isEnabled())
        p.show_completed.setChecked(False)
        self.assertEqual(p.list.count(), 2)
        self.assertIn("请提交材料", p.detail.text())

    def test_empty_state_switches_to_chat_or_retries_storage_failure(self):
        p = self.page
        p.show()
        p.refresh()
        self.assertFalse(p.empty_action.isVisible())
        p.set_chat_available(True)
        self.assertTrue(p.empty_action.isVisible())
        opened = []
        p.request_conversation.connect(lambda: opened.append(True))
        p.empty_action.click()
        self.assertEqual(opened, [True])
        with patch.object(self.store, "list_tasks", side_effect=sqlite3.OperationalError("busy")):
            p.refresh()
        self.assertIn("busy", p.empty_help.text())
        p.set_chat_available(False)
        self.assertTrue(p.empty_action.isVisible())
        p.empty_action.click()
        self.assertEqual(opened, [True])
        self.assertFalse(p.empty_action.isVisible())

    def test_small_window_long_evidence_scrolls_and_controls_fit(self):
        self.store.add_task("很长的待办事项" * 20, self.now, self.item, "一段很长的邮件原文。" * 58)
        p = self.page
        p.refresh()
        p.resize(390, 305)
        p.show()
        self.app.processEvents()
        self.assertEqual(p.width(), 390)
        self.assertGreaterEqual(p.list.viewport().height(), p.list.sizeHintForRow(0))
        self.assertGreater(p.excerpt.verticalScrollBar().maximum(), 0)
        for button in (p.open_source, p.complete, p.show_completed):
            self.assertTrue(button.isVisible())
            self.assertTrue(p.rect().contains(button.mapTo(p, QPoint(0, 0))))
            self.assertTrue(p.rect().contains(button.mapTo(p, button.rect().bottomRight())))
