import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from maildesk.reminder_popup import ReminderPopup


class ReminderPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.owner = QWidget()
        self.popup = ReminderPopup(self.owner)
        self.owner.showMinimized()
        self.addCleanup(self.owner.deleteLater)
        self.addCleanup(self.popup.close)

    def test_adds_batches_while_visible_and_opens_once(self):
        opened = QSignalSpy(self.popup.openRequested)
        self.popup.notify(1)
        self.popup.notify(2)
        self.assertTrue(self.popup.isVisible())
        self.assertTrue(self.owner.isMinimized())
        self.assertTrue(self.popup.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertIn("3 封", self.popup.message.text())
        self.popup.findChild(QPushButton, "viewReminder").click()
        self.assertFalse(self.popup.isVisible())
        self.assertEqual(opened.count(), 1)

        self.popup.notify(2)
        self.assertIn("2 封", self.popup.message.text())
        self.popup.findChild(QPushButton, "dismissReminder").click()
        self.assertFalse(self.popup.isVisible())
        self.assertEqual(opened.count(), 1)

    def test_timeout_and_empty_check(self):
        self.popup.notify(0)
        self.assertFalse(self.popup.isVisible())
        self.popup._timer.setInterval(10)
        timeout = QSignalSpy(self.popup._timer.timeout)
        self.popup.notify(1)
        self.assertTrue(timeout.wait(1000))
        self.assertFalse(self.popup.isVisible())
