"""Conversation navigation within the selected mailbox scope."""

from collections import Counter
from html import escape

from PySide6.QtCore import QRect, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QPushButton, QSizePolicy

from .mail_visuals import icon


class _ConversationSelector(QComboBox):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QColor("#397fc0" if self.hasFocus() else "#d6dde5"))
        painter.drawRoundedRect(QRectF(area), 6, 6)
        center = area.center().y()
        icon("conversation", "#476883", 16).paint(painter, QRect(9, center - 8, 16, 16))
        text_area = area.adjusted(32, 0, -24, 0)
        title = self.currentText() if self.currentIndex() >= 0 else self.placeholderText()
        title = self.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, text_area.width())
        painter.setPen(QColor("#2b3847"))
        painter.drawText(text_area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
        if self.isEnabled():
            icon("chevron", "#54697c", 14).paint(painter, QRect(area.right() - 19, center - 7, 14, 14))


class ConversationBar(QFrame):
    new_requested = Signal()
    selected = Signal(int)
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("conversationBar")
        self.setStyleSheet("""
            QFrame#conversationBar { background: #f7f8fa; border-bottom: 1px solid #e3e7eb; }
            QPushButton { border: 1px solid transparent; border-radius: 6px;
                          padding: 4px 8px; background: transparent; color: #465260; }
            QPushButton#newConversation { color: #1d609e; background: #e9f1fa; }
            QPushButton#newConversation:hover { background: #dceafb; }
            QPushButton#deleteConversation:hover { background: #faeae7; color: #97392d; }
            QPushButton:disabled { color: #7e8791; background: transparent; }
            QPushButton:focus { border: 1px solid #397fc0; }
            QComboBox QAbstractItemView { background: white; color: #263442;
                selection-background-color: #e1eefa; selection-color: #174b7f; }
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 6, 12, 6)
        row.setSpacing(6)
        self.selector = _ConversationSelector()
        self.selector.setObjectName("conversationSelector")
        self.selector.setAccessibleName("切换对话")
        self.selector.setPlaceholderText("新对话")
        self.selector.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.selector.setMinimumContentsLength(3)
        self.selector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.selector.setFixedHeight(29)
        row.addWidget(self.selector, 1)

        self.new_button = QPushButton(icon("plus", "#2769a6", 14), "新建对话")
        self.new_button.setObjectName("newConversation")
        self.delete_button = QPushButton(icon("trash", "#697584", 14), "删除")
        self.delete_button.setObjectName("deleteConversation")
        self.delete_button.setAccessibleName("删除当前对话")
        self.delete_button.setToolTip("删除当前对话")
        for button in (self.new_button, self.delete_button):
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setIconSize(QSize(14, 14))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)

        self.selector.currentIndexChanged.connect(self._select)
        self.new_button.clicked.connect(self.new_requested)
        self.delete_button.clicked.connect(self.delete_requested)
        self.set_conversations((), None)

    def set_conversations(self, conversations, current_id):
        conversations = tuple(conversations)
        titles = Counter(conversation.title for conversation in conversations)
        with QSignalBlocker(self.selector):
            self.selector.clear()
            for conversation in conversations:
                title = conversation.title
                if titles[title] > 1:
                    title = f"#{conversation.id}  {title}"
                self.selector.addItem(title, conversation.id)
            index = self.selector.findData(current_id) if current_id is not None else -1
            self.selector.setCurrentIndex(index)
        self.selector.setEnabled(bool(self.selector.count()))
        self._update_selection()

    def _update_selection(self):
        active = self.selector.currentIndex() >= 0
        self.delete_button.setEnabled(active)
        title = self.selector.currentText() if active else "新对话"
        self.selector.setToolTip(f"<p>{escape(title)}</p>")

    def _select(self, index):
        self._update_selection()
        if index >= 0:
            self.selected.emit(self.selector.itemData(index))
