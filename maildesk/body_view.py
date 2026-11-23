"""Read text immediately and start the HTML engine on the first rich message."""

from PySide6.QtCore import QByteArray, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QStackedWidget, QTextBrowser

from .mail_html import build_mail_html, plain_body_html, safe_link


class _TextView(QTextBrowser):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setAcceptDrops(False)
        self.document().setDocumentMargin(18)
        font = self.font()
        font.setPixelSize(15)
        self.setFont(font)
        self.anchorClicked.connect(self._open_link)

    def loadResource(self, kind, url):
        return QByteArray()

    @staticmethod
    def _open_link(url):
        if safe_link(url.toString()):
            QDesktopServices.openUrl(url)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        link = safe_link(self.anchorAt(event.pos()))
        if link:
            menu.addAction("在浏览器中打开链接", lambda: self._open_link(QUrl(link)))
            menu.addAction("复制链接地址", lambda: QApplication.clipboard().setText(link))
        if self.textCursor().hasSelection():
            menu.addAction("复制", self.copy)
        if not menu.isEmpty():
            menu.exec(event.globalPos())


class MessageBodyView(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._plain = _TextView(self)
        self._plain_text = ""
        self.addWidget(self._plain)
        self._web = None
        self._web_available = True
        self._release = QTimer(self)
        self._release.setSingleShot(True)
        self._release.setInterval(5_000)
        self._release.timeout.connect(self._release_web)

    def _show_text_view(self):
        if self._web is not None and self.currentWidget() is self._web:
            self.setCurrentWidget(self._plain)
            self._web.release_document()
            # Reuse the engine if the user is just switching between HTML messages.
            self._release.start()

    def _release_web(self):
        if self._web is not None and self.currentWidget() is self._plain:
            self.removeWidget(self._web)
            self._web.deleteLater()
            self._web = None

    def setPlainText(self, text):
        self._show_text_view()
        self._plain_text = text
        if "://" in text:
            self._plain.setHtml(plain_body_html(text))
        else:
            self._plain.setPlainText(text)

    def clear(self):
        self.setPlainText("")

    def display_body(self, content):
        if not content.html:
            self.setPlainText(content.text)
            return
        if self._web is None and self._web_available:
            try:
                from .web_reader import WebMailView
            except (ImportError, OSError):
                self._web_available = False
            else:
                self._web = WebMailView(self)
                self.addWidget(self._web)
        if self._web is None:
            self._plain_text = None
            self._plain.setHtml(build_mail_html(content))
            return
        self._release.stop()
        self._plain_text = None
        self._plain.clear()
        self.setCurrentWidget(self._web)
        self._web.display_body(content)

    def toPlainText(self):
        if self._plain_text is not None:
            return self._plain_text
        return self.currentWidget().toPlainText()

    @property
    def remote_urls(self):
        return self._web.remote_urls if self.currentWidget() is self._web else ()

    def set_remote_images(self, images, *, attempted_urls=()):
        if self.currentWidget() is self._web:
            return self._web.set_remote_images(images, attempted_urls=attempted_urls)
        return 0
