import os
import subprocess
import sys
import textwrap
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maildesk.body_view import MessageBodyView
from maildesk.mail_client import _parse_body
from maildesk.models import MailBody
from tests.mail_fixtures import PNG


FIXTURE = Path(__file__).parent / "fixtures" / "github_notification.html"
PULL_REQUEST = "https://github.com/octo-example/mail-reader/pull/42"


class BodyViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = MessageBodyView()
        self.view.resize(700, 500)
        self.view.show()
        self.addCleanup(self.view.close)

    def click_text_link(self, label, url):
        cursor = self.view.document().find(label)
        self.assertFalse(cursor.isNull(), label)
        cursor.setPosition(cursor.selectionStart() + 1)
        self.view.setTextCursor(cursor)
        self.view.ensureCursorVisible()
        self.app.processEvents()
        pos = self.view.cursorRect(cursor).center()
        self.assertEqual(self.view.anchorAt(pos), url)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=pos)

    def test_plain_text_preserves_formatting_and_opens_only_web_links(self):
        text = "详情\n  https://example.org/path?source=mail\n\n"
        self.view.display_body(MailBody(text))
        self.assertEqual(self.view.toPlainText(), text)
        self.assertEqual(self.view.remote_urls, ())
        with patch("maildesk.body_view.QDesktopServices.openUrl") as opened:
            self.click_text_link("https://example.org", "https://example.org/path?source=mail")
            self.view.anchorClicked.emit(QUrl("file:///Users/example/document"))
            opened.assert_called_once_with(QUrl("https://example.org/path?source=mail"))

    def test_notification_table_and_link_open_in_browser_and_keep_message_visible(self):
        self.view.display_body(MailBody("Review requested", FIXTURE.read_text(), {"cid:avatar": PNG}))
        document = self.view.document()
        table = document.find("requested your review").currentTable()
        self.assertIsNotNone(table)
        self.assertEqual(table.columns(), 2)
        self.assertIn("Octo", table.cellAt(0, 1).firstCursorPosition().block().text())
        with patch("maildesk.body_view.QDesktopServices.openUrl") as opened:
            self.click_text_link("View pull request", PULL_REQUEST)
            opened.assert_called_once_with(QUrl(PULL_REQUEST))
        self.assertIs(self.view.document(), document)
        self.assertIn("Notification settings", self.view.toPlainText())

    def test_multipart_comment_shows_html_footer_and_opens_exact_comment_url(self):
        message = EmailMessage()
        message.set_content("Plain-text version of the notification")
        url = PULL_REQUEST + "?via=email&reason=mention#issuecomment-12345"
        message.add_alternative(
            '<p>Could you review the latest change?</p>'
            '<p>Reply to this email directly or '
            f'<a href="{url.replace("&", "&amp;")}">view it on GitHub</a>.</p>'
            '<span style="display:none">Message ID: &lt;sample-comment-12345@github.com&gt;</span>',
            subtype="html", cte="quoted-printable",
        )
        self.view.display_body(_parse_body(message.as_bytes(), rich=True))
        visible = self.view.toPlainText()
        self.assertIn("Could you review the latest change?", visible)
        self.assertIn("Reply to this email directly or view it on GitHub.", visible)
        self.assertNotIn("Message ID:", visible)
        self.assertNotIn("Plain-text version", visible)
        with patch("maildesk.body_view.QDesktopServices.openUrl") as opened:
            self.click_text_link("view it on GitHub", url)
            opened.assert_called_once_with(QUrl(url))

    def test_app_and_reading_html_never_attempt_to_import_webengine(self):
        # A full developer installation must take the same path as an Essentials-only install.
        code = textwrap.dedent("""
            import sys

            class NoWebEngine:
                def find_spec(self, fullname, *args):
                    if fullname.startswith("PySide6.QtWebEngine"):
                        raise AssertionError(fullname)
                    return None

            sys.meta_path.insert(0, NoWebEngine())
            from PySide6.QtWidgets import QApplication
            from maildesk import __main__
            from maildesk.models import MailBody
            from maildesk.reader import ReaderPane

            app = QApplication([])
            pane = ReaderPane()
            pane.set_content(MailBody("", '<a href="https://example.org">首次图文阅读</a>'))
            assert "首次图文阅读" in pane.body.toPlainText()
            pane.clear()
            pane.set_content(MailBody("新的纯文本"))
            assert pane.body.toPlainText() == "新的纯文本"
        """)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
