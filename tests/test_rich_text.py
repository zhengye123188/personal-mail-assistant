import base64
import os
import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt, QUrl
from PySide6.QtGui import QImage, QPixmap, QTextDocument
from PySide6.QtWidgets import QApplication

from maildesk.body_view import MessageBodyView
from maildesk.mail_client import _parse_body
from maildesk.models import MailBody
from tests.mail_fixtures import PNG


def red_png(width, height):
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
        self.view = MessageBodyView()
        self.view.resize(340, 550)
        self.view.show()
        self.addCleanup(self.view.close)

    def image_names(self):
        names = []
        block = self.view.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                fmt = fragment.charFormat()
                if fmt.isImageFormat():
                    names.extend([fmt.toImageFormat().name()] * fragment.length())
                iterator += 1
            block = block.next()
        return names

    def image_size(self, url):
        resource = self.view.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(url))
        if isinstance(resource, (QImage, QPixmap)) and not resource.isNull():
            return resource.width(), resource.height()
        return None

    def test_cid_attachment_decodes_and_scales_on_resize(self):
        message = EmailMessage()
        message.set_content("备选")
        message.add_alternative('<img src="CID:%3Cfigure%40mail%3E">', subtype="html")
        message.get_payload()[-1].add_related(red_png(800, 400), maintype="image", subtype="png", cid="<figure@mail>")
        self.view.display_body(_parse_body(message.as_bytes(), rich=True))
        name, = self.image_names()
        self.assertEqual(self.image_size(name), (800, 400))
        doc = self.view.document()
        for width in (340, 200):
            self.view.resize(width, 550)
            self.app.processEvents()
            layout = doc.documentLayout().blockBoundingRect(doc.firstBlock())
            self.assertGreater(layout.height(), (width - 60) / 2)
            self.assertLess(layout.height(), width / 2 + 4)
            self.assertLessEqual(doc.idealWidth(), self.view.viewport().width() + 1)

    def test_declared_percentage_image_width_resizes_with_document(self):
        self.view.display_body(MailBody("", '<img src="cid:photo" width="50%">', {"cid:photo": red_png(800, 400)}))
        doc = self.view.document()
        for width in (340, 220):
            self.view.resize(width, 550)
            self.app.processEvents()
            height = doc.documentLayout().blockBoundingRect(doc.firstBlock()).height()
            available = self.view.viewport().width() - 2 * doc.documentMargin()
            self.assertAlmostEqual(height, available / 4, delta=2)

    def test_data_and_mime_locations_render_without_remote_download(self):
        encoded = base64.b64encode(PNG).decode()
        html = (f'<img src="data:image/png;base64,{encoded}"><img src="images/logo.png">'
                '<img src="https://example.org/saved.png"><img src="//example.org/remote.png" alt="海报">')
        self.view.display_body(MailBody("备选", html, {"images/logo.png": PNG, "https://example.org/saved.png": PNG}))
        self.assertEqual(self.view.remote_urls, ("https://example.org/remote.png",))
        names = self.image_names()
        self.assertEqual([self.image_size(name) for name in names[:3]], [(2, 1)] * 3)
        self.assertFalse(any(name.startswith(("http:", "https:", "data:")) for name in names))

    def test_external_image_retries_update_current_document_and_keep_scroll(self):
        url = "https://example.org/hero.png"
        html = "<p>段落</p>" * 120 + f'<img src="{url}" alt="日程表"><img src="{url}" alt="详情">'
        self.view.display_body(MailBody("备选", html))
        self.app.processEvents()
        scroll = self.view.verticalScrollBar()
        scroll.setValue(180)
        self.assertEqual(scroll.value(), 180)
        self.assertEqual(self.view.remote_urls, (url,))
        self.assertEqual(self.view.set_remote_images({url: b"invalid", "https://other.org/icon": PNG}), 0)
        self.assertEqual(self.view.remote_urls, (url,))
        self.assertIn("日程表", self.view.toPlainText())
        self.assertEqual(self.view.set_remote_images({url: PNG}, attempted_urls=(url,)), 1)
        names = self.image_names()
        self.assertEqual(len(names), 2)
        self.assertEqual([self.image_size(name) for name in names], [(2, 1), (2, 1)])
        self.assertEqual(self.view.remote_urls, ())
        self.assertEqual(scroll.value(), 180)
        self.view.display_body(MailBody("备选", html))
        self.assertEqual(self.view.remote_urls, (url,))
        self.assertIsNone(self.image_size(names[0]))
        self.view.setPlainText("正在读取正文…")
        self.assertEqual(self.view.remote_urls, ())
        self.assertEqual(self.view.set_remote_images({url: PNG}), 0)
        self.assertEqual(self.view.toPlainText(), "正在读取正文…")

    def test_relative_images_prefer_mime_resources_and_use_declared_web_base(self):
        images = ('<img src="logo.png"><img src="inline.png"><img src="../news.png">'
                  '<img src="//assets.example.org/banner.png"><img src="file:///tmp/denied.png">')
        mime_url = "https://mail.example.org/issue/body.html"
        html_base = '<head><base href="https://static.example.org/november/"></head>'
        self.view.display_body(MailBody("正文", html_base + images, {
            "logo.png": PNG, "https://static.example.org/november/inline.png": PNG}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://static.example.org/news.png", "https://assets.example.org/banner.png"))
        self.assertEqual([self.image_size(name) for name in self.image_names()[:2]], [(2, 1), (2, 1)])
        self.view.display_body(MailBody("", '<head><base href="../pics/"></head><img src="hero.png">', {}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://mail.example.org/pics/hero.png",))
        self.view.display_body(MailBody("", '<img src="hero.png">', {}, mime_url))
        self.assertEqual(self.view.remote_urls, ("https://mail.example.org/issue/hero.png",))
        self.view.display_body(MailBody("", '<base href="file:///tmp/"><img src="hero.png">'))
        self.assertEqual(self.view.remote_urls, ())

    def test_file_images_and_non_raster_mime_are_never_loaded(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.png"
            path.write_bytes(PNG)
            html = ('<p>正文</p><script>hidden-script</script>'
                    f'<img src="{path.as_uri()}" alt="本机图片"><img src="cid:wrong" alt="无效图片">')
            self.view.display_body(MailBody("备选", html, {"cid:wrong": b"<svg/>"}))
            self.assertIn("正文", self.view.toPlainText())
            self.assertNotIn("hidden-script", self.view.toPlainText())
            self.assertNotIn(path.as_uri(), self.image_names())
            self.assertIsNone(self.image_size(path.as_uri()))
            self.assertEqual(self.view.remote_urls, ())
