"""Render each email as an isolated, in-memory browser document."""

import json
from dataclasses import replace

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob, QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu
from shiboken6 import isValid

from .mail_html import build_mail_html, safe_link
from .mail_images import data_image, decode_raster
from .models import MailBody, normalize_cid


_ORIGIN = "maildesk://message"
_MAX_IMAGE_BYTES = 48 * 1024 * 1024

# Register before creating the first WebEngine profile; text-only windows never import this module.
_scheme = QWebEngineUrlScheme(b"maildesk")
_scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
_scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme)
QWebEngineUrlScheme.registerScheme(_scheme)


class _OnlyMailResources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.scheme() != "maildesk" or url.host() != "message":
            info.block(True)


class _MessageStore(QWebEngineUrlSchemeHandler):
    """Only the current message and its decoded raster images are addressable."""

    def __init__(self, parent):
        super().__init__(parent)
        self.generation = 0
        self.files = {}
        self.remote = {}
        self.states = {}
        self.waiters = {}
        self.decoded_bytes = 0

    def reset(self):
        for jobs in self.waiters.values():
            for job in jobs:
                if isValid(job):
                    job.fail(QWebEngineUrlRequestJob.Error.RequestAborted)
        self.waiters.clear()
        self.files.clear()
        self.remote.clear()
        self.states.clear()
        self.decoded_bytes = 0
        self.generation += 1

    @property
    def body_path(self):
        return f"/{self.generation}/body"

    def put_image(self, path, raw):
        if self.decoded_bytes >= _MAX_IMAGE_BYTES:
            return False
        image = decode_raster(raw)
        if image is None or self.decoded_bytes + image.sizeInBytes() > _MAX_IMAGE_BYTES:
            return False
        # Serve the validated, size-limited first frame rather than the sender's
        # original file (which may be animated or much larger than its preview).
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            return False
        self.files[path] = (b"image/png", buffer.data())
        self.decoded_bytes += image.sizeInBytes()
        return True

    def _answer(self, job, path):
        if path in self.files:
            mime, data = self.files[path]
            device = QBuffer(job)
            device.setData(data)
            device.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(mime, device)
        else:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)

    def requestStarted(self, job):
        url = job.requestUrl()
        if job.requestMethod() != b"GET" or url.host() != "message":
            job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
            return
        path = url.path()
        if self.states.get(path) == "pending":
            # Chromium can paint and lay out the text while our existing image
            # downloader fetches these URLs; its renderer never contacts them.
            self.waiters.setdefault(path, []).append(job)
        else:
            self._answer(job, path)

    def complete_image(self, path, state):
        self.states[path] = state
        for job in self.waiters.pop(path, []):
            if isValid(job):
                self._answer(job, path)


class _MailPage(QWebEnginePage):
    def __init__(self, profile, store, parent):
        super().__init__(profile, parent)
        self.store = store
        self.newWindowRequested.connect(self._open_user_link)

    def acceptNavigationRequest(self, url, kind, main_frame):
        if not main_frame:
            return False
        if url.scheme() == "maildesk" and url.host() == "message" and url.path() == self.store.body_path:
            return True
        if kind == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and safe_link(url.toString()):
            QDesktopServices.openUrl(url)
        return False

    def _open_user_link(self, request):
        url = request.requestedUrl()
        if request.isUserInitiated() and safe_link(url.toString()):
            QDesktopServices.openUrl(url)


class WebMailView(QWebEngineView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._uses_html = False
        profile = QWebEngineProfile()
        self._store = _MessageStore(profile)
        self._interceptor = _OnlyMailResources(profile)
        profile.setUrlRequestInterceptor(self._interceptor)
        profile.installUrlSchemeHandler(b"maildesk", self._store)
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        profile.downloadRequested.connect(lambda download: download.cancel())
        settings = profile.settings()
        for attr in ("JavascriptEnabled", "JavascriptCanOpenWindows", "JavascriptCanAccessClipboard",
                     "LocalContentCanAccessRemoteUrls", "LocalContentCanAccessFileUrls",
                     "LocalStorageEnabled", "PluginsEnabled", "DnsPrefetchEnabled", "PdfViewerEnabled",
                     "HyperlinkAuditingEnabled", "NavigateOnDropEnabled", "FullScreenSupportEnabled"):
            settings.setAttribute(getattr(QWebEngineSettings.WebAttribute, attr), False)
        settings.setUnknownUrlSchemePolicy(QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)
        page = _MailPage(profile, self._store, self)
        profile.setParent(page)
        self.setPage(page)
        self.setAcceptDrops(False)
        self.loadFinished.connect(self._document_ready)

    def display_body(self, content):
        store = self._store
        store.reset()
        self._text = content.text
        self._uses_html = bool(content.html)
        next_image_id = 0
        if content.inline_images:
            inline = {normalize_cid(key): raw for key, raw in content.inline_images.items()}
            content = replace(content, inline_images=inline)

        def image_url(source):
            nonlocal next_image_id
            path = f"/{store.generation}/image/{next_image_id}"
            next_image_id += 1
            if source in content.inline_images:
                available = store.put_image(path, content.inline_images[source])
            elif source.lower().startswith("data:"):
                available = store.put_image(path, data_image(source))
            elif safe_link(source):
                store.remote[source] = path
                store.states[path] = "pending"
                available = True
            else:
                available = False
            return _ORIGIN + path if available else None

        document = build_mail_html(content, image_url)
        store.files[store.body_path] = (b"text/html", QByteArray(document.encode("utf-8")))
        self.setUrl(QUrl(_ORIGIN + store.body_path))
        self.history().clear()

    def setPlainText(self, text):
        self.display_body(MailBody(text))

    def clear(self):
        self.setPlainText("")

    def release_document(self):
        """Drop pending image requests and bytes when the native text page takes over."""
        self.stop()
        self._store.reset()
        self._uses_html = False
        self._text = ""

    def toPlainText(self):
        return self._text

    @property
    def remote_urls(self):
        return tuple(self._store.remote)

    def set_remote_images(self, images, *, attempted_urls=()):
        store = self._store
        attempts = set(attempted_urls)
        changes = {}
        count = 0
        for source, path in tuple(store.remote.items()):
            if source in images and store.put_image(path, images[source]):
                # Chromium caches a broken image in the live document. Give a
                # successful retry a fresh src without reloading the page.
                state = "retry" if store.states[path] == "failed" else "ready"
                del store.remote[source]
                count += 1
            elif source in attempts or source in images:
                state = "failed"
            else:
                continue
            store.complete_image(path, state)
            changes[path] = state
        self._update_image_elements(changes)
        return count

    def _document_ready(self, ok):
        generation = self._store.generation
        if not ok or self.page().url().path() != self._store.body_path:
            return
        # A fast download can finish before the frame has parsed its images.
        self._update_image_elements(self._store.states)
        if not self._uses_html:
            return

        def remember_visible_text(text):
            if isValid(self) and self._store.generation == generation and text.strip():
                self._text = text

        self.page().toPlainText(remember_visible_text)

    def _update_image_elements(self, states):
        if not states:
            return
        # Only local paths generated by _MessageStore enter this isolated
        # world. Sender scripts stay disabled in the document's main world.
        script = """
            ((states) => {
                for (const img of document.images) {
                    if (!img.hasAttribute('src')) continue;
                    const url = new URL(img.src);
                    const state = states[url.pathname];
                    if (!state || state === 'pending') continue;
                    if (state === 'failed') {
                        img.dataset.maildeskImage = 'failed';
                        if (!img.alt) {
                            img.alt = '图片未能显示';
                            img.dataset.maildeskFallback = 'true';
                        }
                    } else {
                        delete img.dataset.maildeskImage;
                        if (img.dataset.maildeskFallback) {
                            img.alt = '';
                            delete img.dataset.maildeskFallback;
                        }
                        if (state === 'retry' && !url.search) {
                            url.search = '?retry';
                            img.src = url.href;
                        }
                    }
                }
            })(%s)
        """ % json.dumps(states)
        self.page().runJavaScript(script, QWebEngineScript.ScriptWorldId.ApplicationWorld)

    def contextMenuEvent(self, event):
        request = self.lastContextMenuRequest()
        link = request.linkUrl()
        menu = QMenu(self)
        if safe_link(link.toString()):
            menu.addAction("在浏览器中打开链接", lambda: QDesktopServices.openUrl(link))
            menu.addAction("复制链接地址", lambda: QApplication.clipboard().setText(link.toString()))
        if self.hasSelection():
            menu.addAction("复制", lambda: self.triggerPageAction(QWebEnginePage.WebAction.Copy))
        if not menu.isEmpty():
            menu.exec(event.globalPos())
