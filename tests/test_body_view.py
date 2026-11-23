import os
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from maildesk.body_view import MessageBodyView
from maildesk.models import MailBody


class BodyViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_plain_text_preserves_formatting_and_opens_only_web_links(self):
        view = MessageBodyView()
        text = "详情\n  https://example.org/path?source=mail\n\n"
        view.display_body(MailBody(text))
        self.assertEqual(view.toPlainText(), text)
        self.assertIn('href="https://example.org/path?source=mail"', view._plain.document().toHtml())
        self.assertEqual(view.remote_urls, ())
        with patch("maildesk.body_view.QDesktopServices.openUrl") as opened:
            view._plain.anchorClicked.emit(QUrl("https://example.org/path?source=mail"))
            view._plain.anchorClicked.emit(QUrl("file:///Users/example/document"))
            opened.assert_called_once_with(QUrl("https://example.org/path?source=mail"))

    def test_missing_engine_keeps_html_text_links_and_alt_without_loading_resources(self):
        view = MessageBodyView()
        html = ('<head><base href="https://example.org/inbox/"><style>@import "https://no.example";</style></head>'
                '<p style="background-image:url(https://no.example/bg)">账单<a href="invoice">查看明细</a></p>'
                '<img alt="说明图" src="file:///private/example"><script>unwanted</script>')
        with patch.dict(sys.modules, {"maildesk.web_reader": None}):
            view.display_body(MailBody("", html))
            self.assertIn("账单查看明细", view.toPlainText())
            self.assertIn("说明图", view.toPlainText())
            document = view._plain.document().toHtml()
            self.assertIn('href="https://example.org/inbox/invoice"', document)
            for resource in ("file:", "no.example", "<img", "unwanted"):
                self.assertNotIn(resource, document)
            self.assertEqual(view.remote_urls, ())
            view.display_body(MailBody("纯文本账单明细", '<img src="https://no.example">'))
            self.assertEqual(view.toPlainText(), "纯文本账单明细")

    def test_first_html_loads_the_engine_after_app_start_and_releases_its_view(self):
        # Existing rich-reader tests import WebEngine during test discovery. Only a fresh
        # interpreter can catch an eager dependency or a late scheme-registration failure.
        code = textwrap.dedent("""
            import sys
            import time
            from PySide6.QtCore import QCoreApplication, QEvent
            from PySide6.QtTest import QTest
            from PySide6.QtWidgets import QApplication
            from maildesk import __main__
            from maildesk.models import MailBody
            from maildesk.reader import ReaderPane
            app = QApplication([])
            pane = ReaderPane()
            pane.show()
            pane.set_body('正在读取正文…')
            pane.set_content(MailBody('https://example.test'))
            assert not any(key.startswith('PySide6.QtWebEngine') for key in sys.modules)
            pane.set_content(MailBody('', '<a href="https://example.org">首次图文阅读</a>'))
            view = pane.body
            if not view._web_available:
                assert '首次图文阅读' in view.toPlainText()
                sys.exit(0)
            deadline = time.monotonic() + 5
            while not view.toPlainText() and time.monotonic() < deadline:
                QTest.qWait(15)
            assert '首次图文阅读' in view.toPlainText(), view.toPlainText()
            first_web = view._web
            view._release.setInterval(30)
            pane.clear()
            assert not first_web._store.files
            pane.set_content(MailBody('', '<p>第二封</p>'))
            assert view._web is first_web
            pane.set_content(MailBody('新的纯文本'))
            QTest.qWait(75)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            assert view._web is None
            assert view.toPlainText() == '新的纯文本'
        """)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
