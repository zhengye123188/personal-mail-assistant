import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maildesk.conversation_bar import ConversationBar


class ConversationBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bar = ConversationBar()
        self.addCleanup(self.bar.deleteLater)
        self.addCleanup(self.bar.close)
        self.chats = [SimpleNamespace(id=19, title="评审会准备 " * 20),
                      SimpleNamespace(id=7, title="设计评审材料")]
        self.bar.resize(390, self.bar.sizeHint().height())
        self.bar.show()
        self.app.processEvents()

    def test_scope_refresh_and_blank_do_not_activate_a_thread(self):
        selected = []
        b = self.bar
        b.selected.connect(selected.append)
        b.set_conversations(self.chats, 7)
        self.assertEqual(b.selector.currentText(), self.chats[1].title)
        self.assertTrue(b.delete_button.isEnabled())
        b.set_conversations(self.chats, None)
        self.assertEqual(b.selector.currentIndex(), -1)
        self.assertFalse(b.delete_button.isEnabled())
        self.assertTrue(b.selector.isEnabled())
        b.set_conversations((), 7)
        self.assertFalse(b.selector.isEnabled())
        self.assertTrue(b.new_button.isEnabled())
        self.assertFalse(selected)

    def test_long_title_keeps_actions_visible_and_keyboard_activates_by_id(self):
        b = self.bar
        b.set_conversations(self.chats, 19)
        self.app.processEvents()
        self.assertEqual(b.width(), 390)
        for widget in (b.selector, b.new_button, b.delete_button):
            self.assertTrue(b.rect().contains(widget.mapTo(b, QPoint(0, 0))))
            self.assertTrue(b.rect().contains(widget.mapTo(b, widget.rect().bottomRight())))
        selected, actions = [], []
        b.selected.connect(selected.append)
        b.new_requested.connect(lambda: actions.append("new"))
        b.delete_requested.connect(lambda: actions.append("delete"))
        b.selector.setFocus()
        QTest.keyClick(b.selector, Qt.Key.Key_End)
        self.assertEqual(selected, [7])
        b.new_button.setFocus()
        QTest.keyClick(b.new_button, Qt.Key.Key_Space)
        b.delete_button.setFocus()
        QTest.keyClick(b.delete_button, Qt.Key.Key_Space)
        self.assertEqual(actions, ["new", "delete"])

    def test_only_duplicate_titles_have_distinct_labels(self):
        duplicate = SimpleNamespace(id=25, title=self.chats[0].title)
        b = self.bar
        b.set_conversations((*self.chats, duplicate), duplicate.id)
        labels = [b.selector.itemText(i) for i in range(3)]
        self.assertNotEqual(labels[0], labels[2])
        self.assertEqual(labels[1], self.chats[1].title)
        self.assertEqual(b.selector.currentData(), duplicate.id)
