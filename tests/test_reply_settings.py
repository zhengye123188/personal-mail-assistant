import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from maildesk.reply_settings import OFF, ModelSettingsDialog, saved_choice, selected_model


class ReplySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "prefs.ini"
        self.prefs = QSettings(str(self.file), QSettings.Format.IniFormat)
        self.stored = {}

    def dialog(self):
        def run(fn, done):
            try:
                result = fn()
            except Exception as exc:
                done(None, exc)
            else:
                done(result, None)

        dialog = ModelSettingsDialog(None, saved_choice(self.prefs), self.prefs, run)
        dialog.show()
        self.addCleanup(dialog.close)
        return dialog

    @staticmethod
    def save(dialog):
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()

    def test_explicit_opt_in_key_isolation_and_restart(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-env"}), \
                patch("maildesk.reply_settings.KeyringVault") as vault, \
                patch("maildesk.reply_ai._request", return_value={"choices": [{"message": {"content": "已收到"}}]}) as request:
            vault.return_value.set.side_effect = self.stored.__setitem__
            vault.return_value.get.side_effect = self.stored.get
            dialog = self.dialog()
            self.assertEqual(saved_choice(self.prefs), OFF)
            self.assertIsNone(selected_model(OFF, self.prefs))
            self.assertFalse(dialog.enabled.isChecked())
            self.assertFalse(dialog.key.isVisible())
            dialog.enabled.setChecked(True)
            dialog.provider.setCurrentIndex(dialog.provider.findData("deepseek"))
            dialog.model.setCurrentIndex(dialog.model.findData("deepseek-v4-pro"))
            dialog.key.setText("fixture-key-A")
            self.save(dialog)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            reopened = QSettings(str(self.file), QSettings.Format.IniFormat)
            self.assertEqual(saved_choice(reopened), ("deepseek", "deepseek-v4-pro"))
            self.assertNotIn("fixture-key-A", self.file.read_text())
            model = selected_model(saved_choice(reopened), reopened)
            vault.return_value.get.assert_not_called()
            request.assert_not_called()
            self.assertEqual(model.generate("测试", "样例邮件", "确认收到"), "已收到")
            self.assertEqual(request.call_args.args[2], "fixture-key-A")
            dialog = self.dialog()
            self.assertFalse(dialog.key.text())
            dialog.key.setText("never-use-with-another-provider")
            dialog.provider.setCurrentIndex(dialog.provider.findData("dashscope"))
            self.assertFalse(dialog.key.text())
            dialog.key.setText("fixture-key-B")
            self.save(dialog)
            self.assertEqual(self.stored, {"reply-api/deepseek": "fixture-key-A", "reply-api/dashscope": "fixture-key-B"})
            dialog = self.dialog()
            dialog.enabled.setChecked(False)
            self.save(dialog)
            self.assertEqual(saved_choice(self.prefs), OFF)
            self.assertIsNone(selected_model(saved_choice(self.prefs), self.prefs))
            self.assertEqual(request.call_count, 1)

    def test_failed_key_storage_does_not_enable_provider(self):
        with patch("maildesk.reply_settings.KeyringVault") as vault:
            vault.return_value.set.side_effect = RuntimeError("keystore unavailable")
            dialog = self.dialog()
            dialog.enabled.setChecked(True)
            dialog.key.setText("fixture-key")
            self.save(dialog)
            self.assertTrue(dialog.error.isVisible())
            self.assertEqual(saved_choice(self.prefs), OFF)
            self.assertFalse(self.prefs.contains("keys/deepseek"))
