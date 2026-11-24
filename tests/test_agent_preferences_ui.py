import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from maildesk.agent_preferences import PreferencePage
from maildesk.agent_store import AgentPreferences, AgentStore


class PreferencePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = AgentStore(Path(folder.name) / "preferences.sqlite3")

    def page(self):
        page = PreferencePage(self.store)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.close)
        return page

    def test_changed_reverted_failed_and_retried_save(self):
        page = self.page()
        changed = []
        page.changed.connect(lambda: changed.append(self.store.preferences()))
        self.assertFalse(page.save.isEnabled())
        page.detail.setCurrentText("详细")
        self.assertTrue(page.save.isEnabled())
        page.detail.setCurrentText("简洁")
        self.assertFalse(page.save.isEnabled())
        page.focus.setText("  我的会议  ")
        self.assertIn("未保存", page.feedback.text())
        with patch.object(self.store, "set_preferences", side_effect=sqlite3.OperationalError("数据库忙")):
            page.save.click()
        self.assertTrue(page.save.isEnabled())
        self.assertIn("数据库忙", page.feedback.text())
        self.assertFalse(changed)
        page.save.click()
        self.assertEqual(changed, [AgentPreferences(focus="我的会议")])
        self.assertIn("已保存", page.feedback.text())
        self.assertFalse(page.save.isEnabled())
        self.assertEqual(page.focus.text(), "我的会议")

    def test_custom_minutes_presets_and_short_window(self):
        self.store.set_preferences(AgentPreferences(reminder_lead_minutes=127))
        page = self.page()
        page.resize(355, 245)
        page.show()
        self.app.processEvents()
        self.assertTrue(page.lead.isVisibleTo(page))
        self.assertEqual(page.lead.value(), 127)
        editor = page.lead.lineEdit()
        self.assertGreater(editor.width(), editor.fontMetrics().horizontalAdvance("10080 分钟"))
        self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(page.scroll.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(page.focus.width(), 240)
        page.scroll.ensureWidgetVisible(page.lead)
        self.app.processEvents()
        pos = page.lead.mapTo(page.scroll.viewport(), page.lead.rect().center())
        self.assertTrue(page.scroll.viewport().rect().contains(pos))
        self.assertTrue(page.rect().contains(page.save.mapTo(page, QPoint(2, 2))))
        page.lead.setValue(10080)
        page.save.click()
        self.assertEqual(self.store.preferences().reminder_lead_minutes, 10080)
        page.lead_choice.setCurrentIndex(page.lead_choice.findData(0))
        self.assertFalse(page.lead.isVisibleTo(page))
        page.save.click()
        self.assertEqual(self.store.preferences().reminder_lead_minutes, 0)

    def test_failed_load_can_retry_without_replacing_saved_preferences(self):
        self.store.set_preferences(AgentPreferences(language="English", focus="稿件"))
        with patch.object(self.store, "preferences", side_effect=sqlite3.OperationalError("暂时无法读取")):
            page = self.page()
            self.assertFalse(page.scroll.isEnabled())
            self.assertIn("读取", page.save.text())
        page.save.click()
        self.assertTrue(page.scroll.isEnabled())
        self.assertFalse(page.save.isEnabled())
        self.assertEqual(page.language.currentText(), "English")
        self.assertEqual(page.focus.text(), "稿件")
