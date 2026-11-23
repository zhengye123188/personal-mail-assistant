"""The message header, image controls and reading surface."""

from zlib import crc32

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .mail_visuals import icon, sender_display
from .models import MailBody, MailItem
from .web_reader import WebMailView


def _font(widget, pixels, weight=QFont.Weight.Normal):
    font = widget.font()
    font.setPixelSize(pixels)
    font.setWeight(weight)
    return font


def _plain_label(parent, name):
    label = QLabel(parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _SingleLine(QLabel):
    """Draw an ellipsis while keeping the full text available to assistive tools."""

    def __init__(self, parent, name, color):
        super().__init__(parent)
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._color = QColor(color)

    def sizeHint(self):
        return QSize(super().sizeHint().width(), self.fontMetrics().height() + 3)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self._color)
        painter.setFont(self.font())
        line = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(self.rect(), self.alignment() | Qt.AlignmentFlag.AlignVCenter, line)


class _Avatar(QWidget):
    _colors = (
        ("#afc8ee", "#7687c2"), ("#aad4d5", "#6595a3"), ("#c3b7e4", "#8d7cb8"),
        ("#acd0bd", "#719b83"), ("#dfc2b5", "#ad867c"), ("#b5c6db", "#7f93ad"),
    )

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self._sender = ""

    def set_sender(self, sender):
        self._sender = sender.strip()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        light, dark = self._colors[crc32(self._sender.encode("utf-8", "replace")) % len(self._colors)]
        fill = QLinearGradient(5, 0, self.width() - 5, self.height())
        fill.setColorAt(0, QColor(light))
        fill.setColorAt(1, QColor(dark))
        painter.setBrush(fill)
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(_font(self, 17, QFont.Weight.DemiBold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._sender[:1].upper())


class ReaderPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("readerPane")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#readerPane, QWidget#readerArticle, QWidget#emptyReader {
                background: #ffffff; color: #252528;
            }
            QLabel#subjectLabel { color: #252528; background: transparent; }
            QLabel#emptyReaderLabel { color: #87878d; }
            QFrame#readerDivider { background: #dedee2; }
            QLabel#seenStatus { color: #68686f; font-size: 12px; }
            QFrame#imageNotice { background: #f2f5f9; border-radius: 8px; }
            QLabel#imageStatus { color: #4d5868; font-size: 12px; }
            QPushButton#retryBodyButton, QPushButton#completePending, QPushButton#retrySeen,
            QPushButton#loadImages {
                color: #007aff; background: #eef4fc; border: 0; border-radius: 11px;
                padding: 5px 12px;
            }
            QPushButton#retryBodyButton:hover, QPushButton#completePending:hover, QPushButton#retrySeen:hover { background: #e0ebfc; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.stack.addWidget(self._empty())
        self.stack.addWidget(self._article())
        self.clear()

    def _empty(self):
        pane = QWidget()
        pane.setObjectName("emptyReader")
        box = QVBoxLayout(pane)
        box.setSpacing(9)
        box.addStretch()
        envelope = QLabel()
        envelope.setPixmap(icon("envelope", "#babac1", 34).pixmap(34, 34))
        box.addWidget(envelope, 0, Qt.AlignmentFlag.AlignHCenter)
        message = _plain_label(pane, "emptyReaderLabel")
        message.setText("选择一封邮件查看正文")
        message.setFont(_font(message, 14))
        box.addWidget(message, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addStretch()
        return pane

    def _article(self):
        pane = QWidget()
        pane.setObjectName("readerArticle")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(23, 17, 23, 14)
        header.setSpacing(10)
        self.avatar = _Avatar(pane)
        header.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        fields = QVBoxLayout()
        fields.setSpacing(3)
        sender_row = QHBoxLayout()
        sender_row.setSpacing(6)
        self.sender = _SingleLine(pane, "readerSender", "#232326")
        self.sender.setFont(_font(self.sender, 16, QFont.Weight.Bold))
        sender_row.addWidget(self.sender, 1)
        self.folder_icon = QLabel(pane)
        self.folder_icon.setPixmap(icon("inbox", "#929298", 14).pixmap(14, 14))
        sender_row.addWidget(self.folder_icon)
        self.mailbox = _SingleLine(pane, "readerMailbox", "#78787f")
        self.mailbox.setFont(_font(self.mailbox, 12))
        self.mailbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sender_row.addWidget(self.mailbox)
        self.date = _SingleLine(pane, "readerDate", "#78787f")
        self.date.setFont(_font(self.date, 12))
        self.date.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.date.setAlignment(Qt.AlignmentFlag.AlignRight)
        sender_row.addWidget(self.date)
        fields.addLayout(sender_row)
        self.subject = _plain_label(pane, "subjectLabel")
        self.subject.setWordWrap(True)
        self.subject.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.subject.setFont(_font(self.subject, 15, QFont.Weight.Medium))
        self.subject.setMaximumHeight(self.subject.fontMetrics().lineSpacing() * 3 + 4)
        fields.addWidget(self.subject)
        self.meta = _SingleLine(pane, "readerMeta", "#707077")
        self.meta.setFont(_font(self.meta, 13))
        fields.addWidget(self.meta)
        header.addLayout(fields, 1)
        layout.addLayout(header)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setObjectName("readerDivider")
        divider_row = QHBoxLayout()
        divider_row.setContentsMargins(23, 0, 23, 0)
        divider_row.addWidget(divider)
        layout.addLayout(divider_row)

        self.image_notice = QFrame(pane)
        self.image_notice.setObjectName("imageNotice")
        image_row = QHBoxLayout(self.image_notice)
        image_row.setContentsMargins(12, 4, 8, 4)
        self.image_status = _plain_label(self.image_notice, "imageStatus")
        self.image_status.setWordWrap(True)
        self.image_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.load_images = QPushButton("重试载入", self.image_notice)
        self.load_images.setObjectName("loadImages")
        self.load_images.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_images.setToolTip("重新载入这封邮件中未能显示的外部图片")
        image_row.addWidget(self.image_status, 1)
        image_row.addWidget(self.load_images)
        notice_row = QHBoxLayout()
        notice_row.setContentsMargins(17, 0, 17, 0)
        notice_row.addWidget(self.image_notice)
        layout.addLayout(notice_row)

        self.body = WebMailView(pane)
        self.body.setObjectName("bodyText")
        layout.addWidget(self.body, 1)
        self.retry = QPushButton("重新读取正文", pane)
        self.retry.setObjectName("retryBodyButton")
        self.retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seen_status = _plain_label(pane, "seenStatus")
        self.seen_status.setWordWrap(True)
        self.seen_retry = QPushButton("重试同步", pane)
        self.seen_retry.setObjectName("retrySeen")
        self.seen_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seen_retry.setAccessibleName("重试同步已读状态")
        footer = QHBoxLayout()
        footer.setContentsMargins(23, 0, 23, 6)
        footer.addWidget(self.retry)
        footer.addWidget(self.seen_status)
        footer.addWidget(self.seen_retry)
        footer.addStretch()
        self.complete = QPushButton("标为已处理", pane)
        self.complete.setObjectName("completePending")
        self.complete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.complete.setToolTip("从本机待处理列表移除这封邮件")
        footer.addWidget(self.complete)
        layout.addLayout(footer)
        return pane

    def clear(self):
        self.stack.setCurrentIndex(0)
        self.sender.setText("")
        self.subject.clear()
        self.meta.setText("")
        self.mailbox.setText("")
        self.date.setText("")
        self.body.clear()
        self.image_notice.hide()
        self.retry.hide()
        self.set_seen_status()
        self.complete.hide()

    def show_item(self, item: MailItem):
        self.set_seen_status()
        sender = sender_display(item.sender) or "未知发件人"
        self.sender.setText(sender)
        self.sender.setToolTip(item.sender or sender)
        self.avatar.set_sender(sender)
        self.subject.setText(item.subject or "（无主题）")
        self.subject.setToolTip(self.subject.text())
        name = item.account.rpartition("@")[2].partition(".")[0]
        self.mailbox.setText(f"收件箱 – {name.upper()}")
        self.mailbox.setToolTip(item.account)
        self.mailbox.setFixedWidth(min(105, self.mailbox.sizeHint().width() + 1))
        self.date.setText(item.date or "")
        self.date.setFixedWidth(min(145, self.date.sizeHint().width() + 4))
        recipients = item.recipient.strip() or "未列出"
        metadata = f"收件人：{recipients}"
        if item.account.casefold() not in recipients.casefold():
            metadata += f"    所属邮箱：{item.account}"
        self.meta.setText(metadata)
        self.stack.setCurrentIndex(1)
        self.set_body("正在读取正文…")
        self._fit_header()

    def set_body(self, text, error=False):
        self.image_notice.hide()
        self.body.setPlainText(text)
        self.retry.setVisible(error)

    def set_content(self, content: MailBody):
        self.image_notice.hide()
        self.body.display_body(content)
        self.retry.hide()

    def show_image_failures(self):
        remaining = len(self.body.remote_urls)
        if remaining:
            self.image_status.setText(f"{remaining} 张外部图片未能显示。")
        self.image_notice.setVisible(bool(remaining))

    def set_seen_status(self, text="", *, retry=False):
        self.seen_status.setText(text)
        self.seen_status.setVisible(bool(text))
        self.seen_retry.setVisible(retry)

    def _fit_header(self):
        # At compact widths give the sender priority over the mailbox badge.
        wide = self.width() >= 465
        self.folder_icon.setVisible(wide)
        self.mailbox.setVisible(wide)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_header()
