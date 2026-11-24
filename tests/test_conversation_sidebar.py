import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maildesk.conversation_sidebar import ConversationSidebar


class ConversationSidebarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sidebar = ConversationSidebar()
        self.addCleanup(self.sidebar.deleteLater)
        self.addCleanup(self.sidebar.close)
        self.chats = [SimpleNamespace(id=19, title="评审会准备 " * 20),
                      SimpleNamespace(id=7, title="设计评审材料")]
        self.sidebar.resize(212, 400)
        self.sidebar.show()
        self.app.processEvents()

    def test_refresh_shows_threads_in_store_order_without_activating_them(self):
        sidebar = self.sidebar
        view = sidebar.list
        selected = []
        sidebar.selected.connect(selected.append)
        sidebar.set_conversations(self.chats, 7)
        self.assertEqual([view.item(i).data(Qt.ItemDataRole.UserRole) for i in range(view.count())],
                         [19, 7])
        self.assertEqual(view.currentItem().text(), self.chats[1].title)
        sidebar.set_conversations(self.chats, None)
        self.assertFalse(view.selectedItems())
        sidebar.set_conversations((), 7)
        self.assertEqual(view.count(), 0)
        self.assertTrue(sidebar.new_button.isEnabled())
        self.assertFalse(selected)

    def test_body_click_selects_once_and_trash_click_does_not_select(self):
        sidebar = self.sidebar
        view = sidebar.list
        sidebar.set_conversations(self.chats, 19)
        self.app.processEvents()
        selected, deleted = [], []
        sidebar.selected.connect(selected.append)
        sidebar.delete_requested.connect(deleted.append)
        second = view.visualItemRect(view.item(1))
        trash = QPoint(second.right() - 16, second.center().y())
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=trash)
        self.assertEqual(selected, [])
        self.assertEqual(deleted, [7])
        self.assertEqual(view.currentItem().data(Qt.ItemDataRole.UserRole), 19)
        for expected in ([7], [7, 7]):
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=second.center())
            self.assertEqual(selected, expected)

    def test_keyboard_can_switch_delete_and_start_a_conversation(self):
        sidebar = self.sidebar
        view = sidebar.list
        sidebar.set_conversations(self.chats, 19)
        selected, deleted, new = [], [], []
        sidebar.selected.connect(selected.append)
        sidebar.delete_requested.connect(deleted.append)
        sidebar.new_requested.connect(lambda: new.append(True))
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(selected, [7])
        QTest.keyClick(view, Qt.Key.Key_Delete)
        self.assertEqual(deleted, [7])
        sidebar.new_button.setFocus()
        QTest.keyClick(sidebar.new_button, Qt.Key.Key_Space)
        self.assertEqual(new, [True])

    def test_only_duplicate_titles_have_distinct_labels(self):
        duplicate = SimpleNamespace(id=25, title=self.chats[0].title)
        view = self.sidebar.list
        self.sidebar.set_conversations((*self.chats, duplicate), duplicate.id)
        labels = [view.item(i).text() for i in range(view.count())]
        self.assertNotEqual(labels[0], labels[2])
        self.assertEqual(labels[1], self.chats[1].title)
        self.assertEqual(view.currentItem().data(Qt.ItemDataRole.UserRole), duplicate.id)
