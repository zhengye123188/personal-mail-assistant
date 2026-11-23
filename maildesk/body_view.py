"""Display mail text and validated images in a native Qt document."""

from dataclasses import replace

from PySide6.QtCore import QByteArray, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QTextBrowser

from .mail_html import build_mail_html, plain_body_html, safe_link
from .mail_images import data_image, decode_raster
from .models import normalize_cid


_MAX_IMAGE_BYTES = 48 * 1024 * 1024


class _MailDocument(QTextDocument):
    def loadResource(self, kind, url):
        # Only addResource() may supply bytes. This also covers CSS imports and backgrounds.
        return QByteArray()


class MessageBodyView(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setAcceptDrops(False)
        font = self.font()
        font.setPixelSize(15)
        self.setFont(font)
        self._plain_text = ""
        self._generation = 0
        self._remote = {}
        self._failed = set()
        self._decoded_bytes = 0
        self.anchorClicked.connect(self._open_link)
        self._reset_document()

    def _reset_document(self):
        self._generation += 1
        self._remote.clear()
        self._failed.clear()
        self._decoded_bytes = 0
        old = self.document()
        owned_document = isinstance(old, _MailDocument)
        doc = _MailDocument(self)
        doc.setDefaultFont(self.font())
        doc.setDocumentMargin(18)
        self.setDocument(doc)
        self.setUndoRedoEnabled(False)
        if owned_document:
            old.deleteLater()

    def _open_link(self, url):
        if url.isRelative() and url.toString().startswith("#"):
            self.scrollToAnchor(url.fragment())
        elif safe_link(url.toString()):
            QDesktopServices.openUrl(url)

    def contextMenuEvent(self, event):
        menu = QMenu()
        link = safe_link(self.anchorAt(event.pos()))
        if link:
            menu.addAction("在浏览器中打开链接", lambda: self._open_link(QUrl(link)))
            menu.addAction("复制链接地址", lambda: QApplication.clipboard().setText(link))
        if self.textCursor().hasSelection():
            menu.addAction("复制", self.copy)
        if not menu.isEmpty():
            menu.exec(event.globalPos())

    def setPlainText(self, text):
        self._reset_document()
        self._plain_text = text
        if "://" in text:
            self.setHtml(plain_body_html(text))
        else:
            super().setPlainText(text)

    def toPlainText(self):
        return self._plain_text if self._plain_text is not None else super().toPlainText()

    def clear(self):
        self.setPlainText("")

    def _store_image(self, name, raw):
        if self._decoded_bytes >= _MAX_IMAGE_BYTES:
            return False
        image = decode_raster(raw)
        if image is None or self._decoded_bytes + image.sizeInBytes() > _MAX_IMAGE_BYTES:
            return False
        self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
        self._decoded_bytes += image.sizeInBytes()
        return True

    def display_body(self, content):
        if not content.html:
            self.setPlainText(content.text)
            return
        self._reset_document()
        self._plain_text = None
        if content.inline_images:
            inline = {normalize_cid(key): raw for key, raw in content.inline_images.items()}
            content = replace(content, inline_images=inline)

        next_image = 0

        def image_url(source):
            nonlocal next_image
            next_image += 1
            name = f"maildesk://message/{self._generation}/{next_image}"
            if source in content.inline_images:
                return name if self._store_image(name, content.inline_images[source]) else None
            if source.lower().startswith("data:"):
                return name if self._store_image(name, data_image(source)) else None
            if safe_link(source):
                self._remote[source] = name
                return name
            return None

        self.setHtml(build_mail_html(content, image_url))

    @property
    def remote_urls(self):
        return tuple(self._remote)

    def set_remote_images(self, images, *, attempted_urls=()):
        failed = set()
        restored = set()
        count = 0
        for source in set(attempted_urls).union(images):
            name = self._remote.get(source)
            if name is None:
                continue
            if self._store_image(name, images.get(source)):
                del self._remote[source]
                if name in self._failed:
                    self._failed.remove(name)
                    restored.add(name)
                count += 1
            elif name not in self._failed:
                self._failed.add(name)
                failed.add(name)
        if failed or restored:
            self._update_image_labels(failed, restored)
        if count:
            self.document().markContentsDirty(0, self.document().characterCount())
        return count

    def _update_image_labels(self, failed, restored):
        # Keep the original image properties (and surrounding link) in the text
        # format so a retry can swap just this label back into an image.
        doc = self.document()
        edits = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                name = fmt.stringProperty(QTextFormat.Property.ImageName)
                if name in failed or name in restored:
                    label = " ".join(fmt.stringProperty(QTextFormat.Property.ImageAltText).split())[:200] or "图片未能显示"
                    if fmt.isImageFormat() and name in failed:
                        fmt.setObjectType(QTextFormat.ObjectTypes.NoObject)
                        edits.append((fragment.position(), fragment.length(), label * fragment.length(), fmt))
                    elif not fmt.isImageFormat() and name in restored:
                        fmt.setObjectType(QTextFormat.ObjectTypes.ImageObject)
                        count = fragment.text().count(label)
                        edits.append((fragment.position(), fragment.length(), "\ufffc" * count, fmt))
                it += 1
            block = block.next()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for start, length, text, fmt in reversed(edits):
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(text, fmt)
        cursor.endEditBlock()
