import json
import os
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWidgets import QApplication

from maildesk.mail_client import _parse_body
from maildesk.models import MailBody
from maildesk.web_reader import WebMailView
from tests.mail_fixtures import PNG


FIXTURE = Path(__file__).parent / "fixtures" / "github_notification.html"
PULL_REQUEST = "https://github.com/octo-example/mail-reader/pull/42"


class WebReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = WebMailView()
        self.view.resize(720, 480)
        self.view.show()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QTest.qWait(30)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            QTest.qWait(15)
        self.fail("The message did not reach the expected browser state")

    def evaluate(self, expression):
        result = []
        self.view.page().runJavaScript(
            f"JSON.stringify({expression})", QWebEngineScript.ScriptWorldId.ApplicationWorld,
            result.append,
        )
        self.wait_until(lambda: bool(result))
        return json.loads(result[0]) if result[0] else None

    def github_message(self):
        self.view.display_body(MailBody("Review requested", FIXTURE.read_text(), {"cid:avatar": PNG}))
        self.wait_until(lambda: self.evaluate("Boolean(document.querySelector('a.action'))"))

    def click_link(self, selector):
        x, y = self.evaluate(f"""(() => {{
            const rect = document.querySelector({json.dumps(selector)}).getBoundingClientRect();
            return [Math.round(rect.x + rect.width / 2), Math.round(rect.y + rect.height / 2)];
        }})()""")
        target = self.view.focusProxy()
        self.assertIsNotNone(target)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(x, y))

    def test_notification_preserves_table_layout_and_responsive_button_styles(self):
        self.github_message()
        self.wait_until(lambda: self.evaluate("document.querySelector('img').naturalWidth > 0"))
        layout = self.evaluate("""(() => {
            const card = document.querySelector('.card'), button = document.querySelector('.action');
            const style = getComputedStyle(button), table = document.querySelector('.container');
            return {
                width: table.offsetWidth, nestedCells: card.querySelector('table').rows[0].cells.length,
                avatarSize: [card.querySelector('img').offsetWidth, card.querySelector('img').offsetHeight],
                padding: getComputedStyle(card).paddingLeft, buttonColor: style.backgroundColor,
                radius: style.borderRadius, buttonHeight: button.offsetHeight, display: style.display,
                footerLinkColor: getComputedStyle(document.querySelector('.muted a')).color
            };
        })()""")
        self.assertEqual(layout["width"], 540)
        self.assertEqual(layout["nestedCells"], 2)
        self.assertEqual(layout["avatarSize"], [28, 28])
        self.assertEqual(layout["padding"], "24px")
        self.assertEqual(layout["buttonColor"], "rgb(31, 136, 61)")
        self.assertEqual(layout["display"], "inline-block")
        self.assertEqual(layout["radius"], "6px")
        self.assertGreater(layout["buttonHeight"], 27)
        self.assertEqual(layout["footerLinkColor"], "rgb(9, 105, 218)")
        self.view.resize(360, 480)
        self.wait_until(lambda: self.evaluate("getComputedStyle(document.querySelector('.card')).paddingLeft") == "12px")
        self.assertTrue(self.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

    def test_clicking_notification_button_opens_browser_and_keeps_the_mail_visible(self):
        with patch("maildesk.web_reader.QDesktopServices.openUrl", return_value=True) as opened:
            self.github_message()
            opened.assert_not_called()
            document_url = self.view.url()
            self.click_link(".action")
            self.wait_until(lambda: opened.called)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0].toString(), PULL_REQUEST)
            self.assertEqual(self.view.url(), document_url)
            self.assertEqual(self.evaluate("document.querySelector('.action').textContent"), "View pull request")

    def test_multipart_comment_shows_html_footer_and_opens_exact_comment_url(self):
        message = EmailMessage()
        message["From"] = "GitHub <notifications@github.com>"
        message["Subject"] = "Re: [octo-example/mail-reader] Update the reader (#42)"
        message.set_content("Plain-text version of the notification")
        url = PULL_REQUEST + "?via=email&reason=mention#issuecomment-12345"
        message.add_alternative(
            '<div class="email-body"><p>Could you review the latest change?</p></div>'
            '<p class="email-footer">Reply to this email directly or '
            f'<a href="{url.replace("&", "&amp;")}">view it on GitHub</a>.</p>'
            '<span style="display:none">Message ID: &lt;sample-comment-12345@github.com&gt;</span>',
            subtype="html", cte="quoted-printable",
        )
        body = _parse_body(message.as_bytes(), rich=True)
        self.assertIn("Plain-text version", body.text)
        with patch("maildesk.web_reader.QDesktopServices.openUrl", return_value=True) as opened:
            self.view.display_body(body)
            self.wait_until(lambda: self.evaluate("Boolean(document.querySelector('.email-footer a'))"))
            visible = self.evaluate("document.body.innerText")
            self.assertIn("Could you review the latest change?", visible)
            self.assertIn("Reply to this email directly or view it on GitHub.", visible)
            self.assertNotIn("Message ID:", visible)
            self.assertNotIn("Plain-text version", visible)
            opened.assert_not_called()
            self.click_link(".email-footer a")
            self.wait_until(lambda: opened.called)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0].toString(), url)

    def test_external_image_arrival_updates_current_document_without_losing_scroll(self):
        url = "https://assets.example.org/notification.png"
        html = ('<p>Review changes</p>' * 80 +
                f'<img alt="Review diagram" width="300" height="150" src="{url}">')
        self.view.display_body(MailBody("Review changes", html))
        self.wait_until(lambda: self.evaluate("document.querySelectorAll('p').length === 80"))
        self.assertEqual(self.view.remote_urls, (url,))
        self.evaluate("(window.scrollTo(0, 350), window.scrollY)")
        self.wait_until(lambda: self.evaluate("window.scrollY") == 350)
        self.view.set_remote_images({url: PNG}, attempted_urls=(url,))
        self.wait_until(lambda: self.evaluate("document.querySelector('img').naturalWidth > 1"))
        self.assertEqual(self.view.remote_urls, ())
        self.assertEqual(self.evaluate("window.scrollY"), 350)
