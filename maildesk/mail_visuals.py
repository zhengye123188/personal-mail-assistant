"""Mail-style icons and message-list painting for the desktop reader."""

from datetime import datetime, timedelta
from email.utils import parseaddr
from functools import lru_cache

from PySide6.QtCore import QByteArray, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap, QTextLayout, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QStyle, QStyledItemDelegate


_SYMBOLS = {
    "inbox": '<path d="M3 13.5 6.5 6h11l3.5 7.5v4.2a1.8 1.8 0 0 1-1.8 1.8H4.8A1.8 1.8 0 0 1 3 17.7z"/><path d="M3.5 13.5h4.9l1.4 2.7h4.4l1.4-2.7h4.9"/>',
    "sidebar": '<rect x="2.8" y="4.2" width="18.4" height="15.6" rx="3"/><path d="M10 4.6v14.8M5.6 8.3h1.6m-1.6 3h1.6"/>',
    "search": '<circle cx="10.4" cy="10.4" r="6.2"/><path d="m15 15 5 5"/>',
    "filter": '<path d="M3.6 6.8h16.8M6.6 12h10.8M9.3 17.2h5.4"/>',
    "more": '<g fill="currentColor" stroke="none"><circle cx="4.5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19.5" cy="12" r="1.7"/></g>',
    "refresh": '<path d="M19.6 10a7.7 7.7 0 1 0-.8 6.4M19.7 4.8v5.6h-5.6"/>',
    "reply": '<path d="m10 5-7 7 7 7v-5c5 0 8 .7 11 4-.8-6.7-4.5-9-11-9z"/>',
    "copy": '<rect x="8.3" y="7.9" width="11.3" height="13.1" rx="2.3"/><path d="M15.4 5V4.9a2.1 2.1 0 0 0-2.1-2.1H6.4a2.1 2.1 0 0 0-2.1 2.1v9.3a2.1 2.1 0 0 0 2.1 2.1"/>',
    "info": '<circle cx="12" cy="12" r="8.7"/><path d="M12 11v5.4"/><circle cx="12" cy="7.8" r=".9" fill="currentColor" stroke="none"/>',
    "plus": '<path d="M12 4.6v14.8M4.6 12h14.8"/>',
    "key": '<path d="M14.8 4a5.2 5.2 0 0 0-5.2 5.2c0 .6.1 1.2.3 1.8L3.5 17.4v3.1h3.2v-2.3h2.4v-2.4l3-3a5.2 5.2 0 1 0 2.7-8.8Z"/><circle cx="16.4" cy="7.8" r=".8" fill="currentColor" stroke="none"/>',
    "flag": '<path d="M5.1 20.5v-16c4.5-3 8.8 3.3 14 .3v9.4c-5.2 3-9.5-3.3-14-.3"/>',
    "unread": '<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>',
    "checklist": '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="m7.6 12.1 3.1 3.2 5.9-6.5"/>',
    "chevron": '<path d="m6.7 9.3 5.3 5.3 5.3-5.3"/>',
    "previous": '<path d="m6.7 14.7 5.3-5.3 5.3 5.3"/>',
    "next": '<path d="m6.7 9.3 5.3 5.3 5.3-5.3"/>',
    "folder": '<path d="M2.8 7V5.9a1.7 1.7 0 0 1 1.7-1.7h4.1l2.1 2.2h8.6a1.9 1.9 0 0 1 1.9 1.9v9.2a1.9 1.9 0 0 1-1.9 1.9H4.7a1.9 1.9 0 0 1-1.9-1.9V7.1h8.1"/>',
    "envelope": '<rect x="2.5" y="5.1" width="19" height="13.8" rx="2.7"/><path d="m3.8 6.8 7.3 5.6a1.4 1.4 0 0 0 1.8 0l7.3-5.6"/>',
}


@lru_cache(maxsize=160)
def icon(name, color="#646469", size=20):
    """Render vector symbols at standard and Retina resolutions."""
    ink = QColor(color).name()
    shape = _SYMBOLS[name].replace("currentColor", ink)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           f'<g fill="none" stroke="{ink}" stroke-width="1.65" stroke-linecap="round" '
           f'stroke-linejoin="round">{shape}</g></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    return result


def sender_display(value):
    """Use the display name where a structured From header includes one."""
    value = (value or "").strip()
    display, address = parseaddr(value)
    return display or (address if "@" in address else value) or "未知发件人"


def _date_text(item):
    if not item.timestamp:
        return item.date or ""
    try:
        sent = datetime.fromtimestamp(item.timestamp)
    except (OSError, ValueError, OverflowError):
        return item.date or ""
    elapsed = datetime.now().date() - sent.date()
    if not elapsed.days:
        return sent.strftime("%H:%M")
    if elapsed == timedelta(days=1):
        return "昨天"
    if elapsed == timedelta(days=2):
        return "前天"
    return f"{sent.year}/{sent.month}/{sent.day}"


def _font(base, pixels, weight=QFont.Weight.Normal):
    font = QFont(base)
    font.setPixelSize(pixels)
    font.setWeight(weight)
    return font


def _preview_lines(text, font, width):
    """Wrap the first line naturally and elide whatever exceeds the second."""
    text = " ".join(text.split())
    if not text or width <= 0:
        return "", ""
    layout = QTextLayout(text, font)
    options = QTextOption()
    options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(options)
    layout.beginLayout()
    first_line = layout.createLine()
    first_line.setLineWidth(width)
    first_length = first_line.textLength()
    layout.endLayout()
    # Qt text indexes use UTF-16 units; emoji and other non-BMP symbols use two.
    encoded = text.encode("utf-16-le", errors="replace")
    first = encoded[:first_length * 2].decode("utf-16-le").rstrip()
    tail = encoded[first_length * 2:].decode("utf-16-le").lstrip()
    return first, QFontMetrics(font).elidedText(tail, Qt.TextElideMode.ElideRight, width)


class MailDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_address = True

    def sizeHint(self, option, index):
        return QSize(150, 83)

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if item is None:
            return super().paint(painter, option, index)
        painter.save()
        try:
            self._draw_message(painter, option, item)
        finally:
            painter.restore()

    def _draw_message(self, painter, option, item):
        rect = option.rect
        painter.setClipRect(rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, QColor("#ffffff"))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e7f1ff" if selected else "#f6f6f7"))
            painter.drawRoundedRect(QRectF(rect.adjusted(4, 1, -4, -1)), 7, 7)
        x = rect.left() + 29
        y = rect.top()
        width = max(1, rect.right() - x - 16)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if getattr(item, "unread", None) is True:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#0a84ff"))
            painter.drawEllipse(QPointF(x - 12, y + 18), 4.1, 4.1)
        if getattr(item, "flagged", None) is True:
            icon("flag", "#ed931a", 11).paint(painter, QRect(x - 18, y + 31, 12, 12))

        sender_font = _font(option.font, 14, QFont.Weight.DemiBold)
        regular = _font(option.font, 13)
        small = _font(option.font, 12)
        date = _date_text(item)
        date_width = min(width // 2, QFontMetrics(small).horizontalAdvance(date) + 1)
        painter.setFont(small)
        painter.setPen(QColor("#79797e"))
        painter.drawText(QRect(x + width - date_width, y + 7, date_width, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, date)

        line_width = max(0, width - date_width - 8)
        fm = QFontMetrics(sender_font)
        sender = fm.elidedText(sender_display(item.sender), Qt.TextElideMode.ElideRight, line_width)
        painter.setFont(sender_font)
        painter.setPen(QColor("#202124"))
        painter.drawText(QRect(x, y + 7, line_width, 20), align, sender)
        preview = getattr(item, "preview", "") or ""
        if self.show_address and preview:
            address_x = fm.horizontalAdvance(sender) + 9
            available = line_width - address_x
            if available >= 62:
                account_font = _font(option.font, 10)
                account = QFontMetrics(account_font).elidedText(item.account, Qt.TextElideMode.ElideMiddle, available)
                painter.setFont(account_font)
                painter.setPen(QColor("#75757a"))
                painter.drawText(QRect(x + address_x, y + 8, available, 20), align, account)

        subject = QFontMetrics(regular).elidedText(item.subject or "（无主题）", Qt.TextElideMode.ElideRight, width)
        painter.setFont(regular)
        painter.setPen(QColor("#252527"))
        painter.drawText(QRect(x, y + 27, width, 18), align, subject)
        painter.setFont(small)
        painter.setPen(QColor("#727278"))
        if self.show_address and not preview:
            preview = item.account
        for n, line in enumerate(_preview_lines(preview, small, width)):
            if line:
                painter.drawText(QRect(x, y + 46 + 15 * n, width, 16), align, line)
        painter.setPen(QColor("#dedee2"))
        painter.drawLine(x, rect.bottom(), x + width, rect.bottom())
