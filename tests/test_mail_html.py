import unittest
from unittest.mock import Mock

from maildesk.mail_html import build_mail_html, safe_link
from maildesk.models import MailBody


class MailHTMLTests(unittest.TestCase):
    def test_notification_css_button_and_legacy_table_layout(self):
        markup = """<!DOCTYPE html><html lang="en"><head>
            <style type="text/css" media="screen">
              .btn {display:inline-block;background:#1f883d;border-radius:6px;color:white}
              @media (max-width:500px) {.container {width:100%}}
            </style></head><body class="notification" bgcolor="#f6f8fa">
            <table class="container" role="presentation" width="600" cellpadding="16" align="center">
            <tr><td colspan="2" valign="top"><h2>Pull request ready</h2>
            <a class="btn" href="https://github.com/example/repo/pull/5?tab=a&amp;b=c"
               style="padding:8px 16px" target="_blank">View pull request</a>
            </td></tr></table></body></html>"""
        html = build_mail_html(MailBody("fallback", markup), self.fail)
        self.assertIn('.btn {display:inline-block;background:#1f883d', html)
        self.assertIn('@media (max-width:500px)', html)
        self.assertIn('<style media="screen">', html)
        self.assertIn('<body class="notification" bgcolor="#f6f8fa">', html)
        self.assertIn('role="presentation" width="600" cellpadding="16" align="center"', html)
        self.assertIn('<td colspan="2" valign="top"><h2>Pull request ready</h2>', html)
        self.assertIn('<a class="btn" style="padding:8px 16px" href="https://github.com/', html)
        self.assertIn('?tab=a&amp;b=c" rel="noreferrer">View pull request</a>', html)
        self.assertNotIn('target="_blank"', html)

    def test_hidden_preheader_and_static_octicon(self):
        markup = ('<style>.preheader { display:none !important }</style>'
                  '<span class="preheader">Notification ID 123</span>'
                  '<a href="https://github.com/example"><svg width="16" height="16" viewBox="0 0 16 16" '
                  'aria-label="GitHub"><title>GitHub</title><g fill="currentColor">'
                  '<path d="M1 0L2 3Z"/></g><foreignObject><p>untrusted frame</p></foreignObject>'
                  '<image href="https://example.org/logo.svg"/><circle cx="5" cy="5" r="1"/>'
                  '</svg>Open conversation</a>')
        html = build_mail_html(MailBody("", markup), self.fail)
        self.assertIn('.preheader { display:none !important }', html)
        self.assertIn('<span class="preheader">Notification ID 123</span>', html)
        self.assertIn('viewBox="0 0 16 16"', html)
        self.assertIn('<title>GitHub</title><g fill="currentColor"><path d="M1 0L2 3Z">', html)
        self.assertIn('<circle cx="5" cy="5" r="1"></circle></svg>Open conversation</a>', html)
        self.assertNotIn('untrusted frame', html)
        self.assertNotIn('logo.svg', html)

    def test_internal_table_of_contents_stays_in_the_mail_with_a_web_base(self):
        markup = ('<a href="#changes">Changes</a><h3 id="changes">Changes</h3>'
                  '<a name="older"></a><a href="#older">Earlier updates</a>')
        html = build_mail_html(MailBody("", markup, base_url="https://example.org/web.html"), self.fail)
        self.assertIn('href="#changes"', html)
        self.assertIn('href="#older"', html)
        self.assertIn('<a name="older"></a>', html)
        self.assertIn('id="changes"', html)
        self.assertNotIn('https://example.org', html)

    def test_mime_bases_normalized_sources_and_image_buttons(self):
        markup = """<head><base href="../templates/"></head>
          <a href="view"> <img src="CID:%3Cicon%40mail%3E" alt="View"></a>
          <img src="logo.png"><img src="web.png" height="60" width="120">
          <img src="web.png"><img src="//cdn.example.org/icon">
          <img src="data:image/png;base64,abcd"><img src="file:///tmp/missing" alt="Missing">"""
        body = MailBody("text", markup, {"cid:icon@mail": b"i", "logo.png": b"x"},
                        "https://example.org/issues/mail.html")
        resolver = Mock(side_effect=lambda src: "maildesk://message/1/image/1")
        html = build_mail_html(body, resolver)
        self.assertEqual([call.args[0] for call in resolver.call_args_list], [
            "cid:icon@mail", "logo.png", "https://example.org/templates/web.png",
            "https://cdn.example.org/icon", "data:image/png;base64,abcd",
        ])
        self.assertIn('href="https://example.org/templates/view" rel="noreferrer"> <img alt="View" src=', html)
        self.assertIn('height="60" width="120" src="maildesk:', html)
        self.assertEqual(html.partition("<body>")[2].count('data-maildesk-image="pending"'), 3)
        self.assertIn('<img alt="Missing">', html)
        self.assertNotIn("<base", html)

    def test_drop_active_content_and_gate_all_resource_requests(self):
        markup = """<meta http-equiv="refresh" content="5;url=https://example.org">
            <link rel="stylesheet" href="https://example.org/screen.css"><style>
             .card {background: #eee url(https://example.org/background.png)}
            </style><form action="https://example.org"><p onmouseover="eventFn()">Body</p>
            <script>scriptFn()</script><button type="submit" formaction="/path">Send</button></form>
            <iframe src="https://example.org">frame content</iframe>
            <a href="file:///tmp/test" target="_blank">File</a>
            <a href="javascript:void(0)" ping="/collect">Invalid link</a>
            <img src="qrc:/thing"><img src="https://example.org/img" alt="Unavailable"
            srcset="https://example.org/extra 2x" onerror="callback()">"""
        html = build_mail_html(MailBody("", markup), lambda src: None)
        for absent in ("<script", "<iframe", "<form", "<link", "refresh", "srcset=", "onerror=", "formaction=",
                       "onmouseover=", "target=", "href=", "ping=", "scriptFn", "frame content", 'src="'):
            self.assertNotIn(absent, html)
        self.assertIn("<p>Body</p>", html)
        self.assertIn('<button type="button">Send</button>', html)
        self.assertIn('<img alt="Unavailable">', html)
        self.assertIn("default-src 'none'; img-src maildesk:; style-src 'unsafe-inline'", html)
        self.assertEqual(html.count("Content-Security-Policy"), 1)

    def test_safe_link_and_plain_text_fallback(self):
        for url in ("data:text/plain,hi", "file:///Users/reader", "https://user:secret@example.org/",
                    "https://example.org:xyz", "not/absolute", "https://[bad", "https://a\\b/c"):
            self.assertIsNone(safe_link(url))
        plain = 'See (https://example.org/items_(1)).\n<signature>\n详情 https://example.org/i?a=b&c=d。'
        for markup in ("", "<head><title>Mail</title></head><script>ignore()</script>"):
            html = build_mail_html(MailBody(plain, markup), self.fail)
            self.assertIn('href="https://example.org/items_(1)"', html)
            self.assertIn('items_(1)</a>).\n&lt;signature&gt;\n', html)
            self.assertIn('href="https://example.org/i?a=b&amp;c=d"', html)
            self.assertIn('i?a=b&amp;c=d</a>。', html)
            self.assertNotIn("ignore()", html)
