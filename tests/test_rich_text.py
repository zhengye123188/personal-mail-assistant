import base64
import json
import os
import random
import time
import unittest
from email.message import EmailMessage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QCoreApplication, QEvent, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maildesk.mail_client import _parse_body
from maildesk.models import MailBody
from maildesk.web_reader import WebMailView


def red_png(width=800, height=400):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    raw = QByteArray()
    device = QBuffer(raw)
    device.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(device, "PNG")
    return bytes(raw)


class RichTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = WebMailView()
        self.view.resize(340, 550)
        self.view.show()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def dom(self, expression):
        result = []
        self.view.page().runJavaScript(f"JSON.stringify({expression})", 1, result.append)
        end = time.monotonic() + 5
        while not result and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(result, expression)
        return json.loads(result[0]) if result[0] else None

    def wait_dom(self, expression):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            value = self.dom(expression)
            if value:
                return value
            QTest.qWait(15)
        self.fail(expression)

    def test_embedded_images_render_and_scale_with_the_reader(self):
        message = EmailMessage()
        message.set_content("备选")
        message.add_alternative('<img src="CID:%3Cfigure%40mail%3E">', subtype="html")
        message.get_payload()[-1].add_related(red_png(), maintype="image", subtype="png", cid="<figure@mail>")
        self.view.display_body(_parse_body(message.as_bytes(), rich=True))
        self.wait_dom("document.images[0]?.naturalWidth === 800")
        width, height = self.dom("[document.images[0].width, document.images[0].height]")
        self.assertGreater(width, 260)
        self.assertAlmostEqual(width / height, 2, delta=0.03)
        self.view.resize(200, 550)
        self.wait_dom(f"document.images[0].width < {width}")
        self.assertTrue(self.dom("document.documentElement.scrollWidth <= innerWidth"))

    def test_table_and_data_or_location_images_render_without_fetching_urls(self):
        raw = red_png(8, 8)
        encoded = base64.b64encode(raw).decode()
        html = (f'<table><tr><th>项目</th><td><strong>确认</strong></td></tr></table>'
                f'<img src="data:image/png;base64,{encoded}"><img src="images/logo.png">'
                '<img src="https://example.org/saved.png"><img src="//example.org/remote.png" alt="海报">')
        self.view.display_body(MailBody("备选", html, {"images/logo.png": raw, "https://example.org/saved.png": raw}))
        self.assertEqual(self.view.remote_urls, ("https://example.org/remote.png",))
        self.wait_dom("document.images.length === 4 && [...document.images].filter(i=>i.naturalWidth===8).length===3")
        self.assertEqual(self.dom("document.querySelector('table td strong').textContent"), "确认")
        self.assertEqual(self.dom("document.images[3].alt"), "海报")

    def test_deferred_photo_can_be_requested_again_with_identical_pixels(self):
        pixels = random.Random(7).randbytes(512 * 384 * 3)
        photo = QImage(pixels, 512, 384, 512 * 3, QImage.Format.Format_RGB888)
        raw = QByteArray()
        output = QBuffer(raw)
        output.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(photo.save(output, "JPEG", 55))
        jpeg = bytes(raw)
        self.view.display_body(MailBody("", '<img src="cid:photo">', {"cid:photo": jpeg}))
        store = self.view._store
        path, = store._compact_sources
        self.assertNotIn(path, store.files)
        self.wait_dom("document.images[0]?.naturalWidth === 512")
        self.assertNotIn(path, store.files)
        self.assertEqual(store._compact_sources[path], jpeg)

        snapshot = """(() => {
            const canvas = document.createElement('canvas');
            canvas.width = 512; canvas.height = 384;
            canvas.getContext('2d').drawImage(document.images[0], 0, 0);
            return canvas.toDataURL();
        })()"""
        self.assertTrue(self.dom(f"(window.originalPhoto = {snapshot}).startsWith('data:image/png;base64,')"))
        self.dom("(document.images[0].src += '?reload', true)")
        self.wait_dom("document.images[0]?.complete && document.images[0].naturalWidth === 512")
        self.assertTrue(self.dom(f"{snapshot} === window.originalPhoto"))
        self.assertNotIn(path, store.files)
        self.view.display_body(MailBody("下一封"))
        self.assertFalse(store._compact_sources)

    def test_external_images_are_scoped_to_current_document_and_keep_scroll(self):
        url = "https://example.org/hero.png"
        html = "<p>段落</p>" * 120 + f'<img src="{url}"><img src="{url}">'
        self.view.display_body(MailBody("备选", html))
        self.wait_dom("document.images.length === 2")
        self.dom("(window.scrollTo(0,180), true)")
        self.wait_dom("scrollY === 180")
        self.assertEqual(self.view.set_remote_images({url: b"invalid", "https://other.org/icon": red_png(3, 3)}), 0)
        self.assertEqual(self.view.remote_urls, (url,))
        self.assertEqual(self.view.set_remote_images({url: red_png(8, 8)}), 1)
        self.wait_dom("[...document.images].every(i=>i.naturalWidth===8)")
        self.assertEqual(self.dom("scrollY"), 180)
        self.view.display_body(MailBody("备选", html))
        self.assertEqual(self.view.remote_urls, (url,))
        self.view.setPlainText("正在读取正文…")
        self.assertEqual(self.view.remote_urls, ())
        self.assertEqual(self.view.set_remote_images({url: red_png(8, 8)}), 0)
        self.assertEqual(self.view.toPlainText(), "正在读取正文…")

    def test_relative_images_prefer_mime_resources_and_use_declared_web_base(self):
        raw = red_png(6, 6)
        images = ('<img src="logo.png"><img src="inline.png"><img src="../news.png">'
                  '<img src="//assets.example.org/banner.png"><img src="file:///tmp/denied.png">')
        mime_url = "https://mail.example.org/issue/body.html"
        html_base = '<head><base href="https://static.example.org/november/"></head>'
        self.view.display_body(MailBody("正文", html_base + images, {
            "logo.png": raw, "https://static.example.org/november/inline.png": raw}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://static.example.org/news.png", "https://assets.example.org/banner.png"))
        self.wait_dom("[...document.images].filter(i=>i.naturalWidth===6).length === 2")
        self.assertEqual(self.view.set_remote_images({"https://static.example.org/news.png": raw}), 1)
        self.view.display_body(MailBody("", '<head><base href="../pics/"></head><img src="hero.png">', {}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://mail.example.org/pics/hero.png",))
        self.view.display_body(MailBody("", '<img src="hero.png">', {}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://mail.example.org/issue/hero.png",))
        self.view.display_body(MailBody("", '<base href="file:///tmp/"><img src="hero.png">'))
        self.assertEqual(self.view.remote_urls, ())

    def test_percentage_image_width_responds_to_resizing(self):
        self.view.display_body(MailBody("", '<img src="cid:photo" width="50%">', {"cid:photo": red_png()}))
        self.wait_dom("document.images[0]?.naturalWidth === 800")
        ratio = "document.images[0].width / document.body.clientWidth"
        self.assertAlmostEqual(self.dom(ratio), 0.5, delta=0.04)
        original = self.dom("document.images[0].width")
        self.view.resize(220, 550)
        self.wait_dom(f"document.images[0].width < {original}")
        self.assertAlmostEqual(self.dom(ratio), 0.5, delta=0.04)

    def test_remote_image_reserves_space_and_restores_alt_on_failure(self):
        url = "https://example.org/banner.png"
        html = (f'<p>周报</p><p><img src="{url}" width="800" height="400" alt="本周日程表"></p>'
                '<p id="after">活动安排</p>')
        top = "document.getElementById('after')?.getBoundingClientRect().top"
        self.view.display_body(MailBody("周报", html))
        wide = self.wait_dom(top)
        self.view.resize(250, 550)
        self.wait_dom(f"({top}) < {wide - 30}")
        narrow = self.dom(top)
        self.assertGreater(narrow, 120)
        self.assertEqual(self.view.set_remote_images({url: red_png()}, attempted_urls=(url,)), 1)
        self.wait_dom("document.images[0]?.naturalWidth === 800")
        self.assertAlmostEqual(self.dom(top), narrow, delta=2)

        self.view.display_body(MailBody("", html.replace('height="400"', 'height="9999"')))
        self.wait_dom("document.images[0]?.getAttribute('height') !== '400' && document.getElementById('after') !== null")
        self.assertLess(self.dom(top), 620)
        self.view.set_remote_images({}, attempted_urls=(url,))
        self.wait_dom(f"({top}) < 120")
        self.assertEqual(self.view.remote_urls, (url,))
        self.assertTrue(self.dom("document.body.innerText.includes('本周日程表') || document.images[0]?.alt === '本周日程表'"))

    def test_only_message_raster_resources_are_renderable(self):
        html = ('<head><style>.hidden-style {color:red}</style></head><p>正文</p><script>hidden-script</script>'
                '<img src="file:///tmp/local.png" alt="本机图片"><img src="cid:wrong" alt="无效图片">')
        self.view.display_body(MailBody("备选", html, {"cid:wrong": b"<svg/>"}))
        self.wait_dom("document.body?.innerText.includes('正文')")
        self.assertNotIn("hidden-", self.dom("document.body.innerText"))
        self.assertFalse(self.dom("[...document.images].some(i=>i.naturalWidth || i.src.startsWith('file:'))"))
        self.assertEqual(self.view.remote_urls, ())
