"""Mailbox-scoped conversation navigation."""

from collections import Counter
from html import escape

from PySide6.QtCore import QEvent, QRect, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QLabel, QListWidget, QListWidgetItem, QPushButton, QStyle,
    QStyledItemDelegate, QToolTip, QVBoxLayout,
)

from .mail_visuals import icon


def _trash_area(row):
    return QRect(row.right() - 31, row.top(), 32, row.height())


class _HistoryDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(100, 36)

    def paint(self, painter, option, index):
        row = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hovered or focused:
            painter.setPen(QColor("#7ca5cf") if focused else Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e0ecf9" if selected else "#eceff3"))
            painter.drawRoundedRect(row.adjusted(1, 2, -1, -2), 6, 6)
        area = row.adjusted(9, 0, -33, 0)
        title = option.fontMetrics.elidedText(index.data(), Qt.TextElideMode.ElideRight, area.width())
        painter.setPen(QColor("#1c537f" if selected else "#324050"))
        painter.setFont(option.font)
        painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        glyph = QRect(0, 0, 15, 15)
        glyph.moveCenter(_trash_area(row).center())
        icon("trash", "#576d80" if selected or hovered else "#738090", 15).paint(painter, glyph)
        painter.restore()


class _HistoryList(QListWidget):
    selected = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_action = None
        self.activate_on_navigation = True
        self.setObjectName("conversationList")
        self.setAccessibleName("历史对话")
        self.setAccessibleDescription("使用上下方向键切换对话，按删除键删除选中的对话。")
        self.setItemDelegate(_HistoryDelegate(self))
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)

    def _action_at(self, point):
        item = self.itemAt(point)
        if item is None:
            return None
        on_trash = _trash_area(self.visualItemRect(item)).contains(point)
        return item.data(Qt.ItemDataRole.UserRole), on_trash

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_action = self._action_at(event.position().toPoint())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not event.buttons():
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            action = self._action_at(point)
            if action is not None and action == self._pressed_action:
                thread_id, on_trash = action
                if on_trash:
                    self.delete_requested.emit(thread_id)
                else:
                    self.setCurrentItem(self.itemAt(point))
                    self.selected.emit(thread_id)
            self._pressed_action = None
        event.accept()

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            action = self._action_at(event.pos())
            if action is not None and action[1]:
                QToolTip.showText(event.globalPos(), "删除对话", self.viewport())
                return True
        return super().viewportEvent(event)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if item is not None and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            thread_id = item.data(Qt.ItemDataRole.UserRole)
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_requested.emit(thread_id)
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self.selected.emit(thread_id)
                event.accept()
                return
        super().keyPressEvent(event)
        current = self.currentItem()
        if self.activate_on_navigation and current is not None and current is not item:
            self.selected.emit(current.data(Qt.ItemDataRole.UserRole))


class ConversationSidebar(QFrame):
    new_requested = Signal()
    selected = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("conversationSidebar")
        self.setStyleSheet("""
            QFrame#conversationSidebar { background: #f7f8fa; border-right: 1px solid #e0e5ea; }
            QPushButton#newConversation { text-align: left; color: #225c92; background: #e7eef7;
                border: 1px solid transparent; border-radius: 7px; padding: 6px 10px; }
            QPushButton#newConversation:hover { background: #dce9f7; }
            QPushButton#newConversation:focus { border-color: #568ab8; }
            QPushButton#newConversation:disabled { color: #78828e; background: #eceff2; }
            QLabel#conversationHistoryHeading { color: #606e7d; font-size: 12px;
                font-weight: 600; padding-left: 8px; }
            QLabel#emptyConversations { color: #697583; padding: 6px 8px; }
            QListWidget#conversationList { border: none; background: transparent; outline: none; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(4)
        self.new_button = QPushButton(icon("plus", "#2769a6", 15), "新建对话")
        self.new_button.setObjectName("newConversation")
        self.new_button.setAutoDefault(False)
        self.new_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.new_button.setIconSize(QSize(15, 15))
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.new_button)
        layout.addSpacing(11)
        heading = QLabel("历史对话")
        heading.setObjectName("conversationHistoryHeading")
        layout.addWidget(heading)
        self.list = _HistoryList()
        layout.addWidget(self.list, 1)
        self.empty_label = QLabel("暂无历史对话")
        self.empty_label.setObjectName("emptyConversations")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.empty_label, 1)
        self.new_button.clicked.connect(self.new_requested)
        self.list.selected.connect(self.selected)
        self.list.delete_requested.connect(self.delete_requested)
        self._rows = ()
        self.set_conversations((), None)

    def set_compact(self, compact):
        # Browse with the arrow keys before explicitly opening a chat in the narrow layout.
        self.list.activate_on_navigation = not compact

    def set_conversations(self, conversations, current_id):
        conversations = tuple(conversations)
        counts = Counter(thread.title for thread in conversations)
        rows = tuple((thread.id, f"#{thread.id}  {thread.title}" if counts[thread.title] > 1 else thread.title)
                     for thread in conversations)
        with QSignalBlocker(self.list):
            old_scroll = self.list.verticalScrollBar().value()
            old_item = self.list.currentItem()
            old_id = old_item.data(Qt.ItemDataRole.UserRole) if old_item is not None else None
            if rows != self._rows:
                self.list.clear()
                for thread_id, title in rows:
                    item = QListWidgetItem(title, self.list)
                    item.setData(Qt.ItemDataRole.UserRole, thread_id)
                    item.setToolTip(f"<p>{escape(title)}</p>")
                self._rows = rows
            row = next((i for i, (thread_id, _) in enumerate(rows) if thread_id == current_id), -1)
            self.list.setCurrentRow(row)
            if old_id == current_id:
                self.list.verticalScrollBar().setValue(old_scroll)
        self.list.setVisible(bool(rows))
        self.empty_label.setVisible(not rows)
