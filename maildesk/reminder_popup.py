"""A nonactivating desktop reminder shared by all connected mailboxes."""

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .mail_visuals import icon


class ReminderPopup(QWidget):
    openRequested = Signal()

    def __init__(self, owner=None, *, deadlines=False):
        # A transient child of the reader would disappear when it is minimized.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        if owner is not None:
            owner.destroyed.connect(self.deleteLater)
        self.setWindowTitle("个人邮件助手")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._count = 0
        self._deadlines = deadlines
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(10_000)
        self._timer.timeout.connect(self.hide)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("reminderCard")
        outer.addWidget(card)
        line = QHBoxLayout(card)
        line.setContentsMargins(14, 11, 10, 11)
        line.setSpacing(9)

        badge = QLabel()
        badge.setObjectName("reminderIcon")
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icon("envelope", "#ffffff", 21).pixmap(QSize(21, 21), self.devicePixelRatioF()))
        line.addWidget(badge)
        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel("截止待办提醒" if deadlines else "个人邮件助手")
        title.setObjectName("reminderTitle")
        text.addWidget(title)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        text.addWidget(self.message)
        line.addLayout(text, 1)
        view = QPushButton("查看")
        view.setObjectName("viewReminder")
        view.setAccessibleName("查看截止待办" if deadlines else "查看待处理邮件")
        view.clicked.connect(self._open)
        line.addWidget(view)
        dismiss = QPushButton("×")
        dismiss.setObjectName("dismissReminder")
        dismiss.setAccessibleName("关闭提醒")
        dismiss.setToolTip("关闭提醒")
        dismiss.setFixedSize(22, 22)
        dismiss.clicked.connect(self.hide)
        line.addWidget(dismiss, 0, Qt.AlignmentFlag.AlignTop)
        self.setStyleSheet("""
            QFrame#reminderCard { background: #f8f8fa; border: 1px solid #d6d6da; border-radius: 14px; }
            QLabel { color: #26262a; font-size: 13px; }
            QLabel#reminderTitle { font-weight: 650; }
            QLabel#reminderIcon { background: #147de7; border-radius: 8px; }
            QPushButton { border: none; border-radius: 6px; }
            QPushButton#viewReminder { color: #0866bc; background: #e5edf7; padding: 4px 9px; }
            QPushButton#viewReminder:hover { background: #d8e6f6; }
            QPushButton#dismissReminder { color: #65656e; background: transparent; font-size: 16px; }
            QPushButton#dismissReminder:hover { background: #e6e6ea; }
        """)

    def notify(self, count: int):
        """Add newly detected items to the currently visible reminder."""
        if count <= 0:
            return
        self._count += count
        self.message.setText(f"新增 {self._count} 封待处理邮件")
        self._present()

    def notify_deadlines(self, tasks):
        if not tasks:
            return
        self._count += len(tasks)
        first = tasks[0]
        title = " ".join(first.title.split())
        title = self.message.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 230)
        when = first.due_at.astimezone().strftime("%m/%d %H:%M")
        suffix = f"（共 {self._count} 项）" if self._count > 1 else ""
        self.message.setText(f"{title} · {when}{suffix}")
        self._present()

    def _present(self):
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            top = area.bottom() - self.height() - 13 if self._deadlines else area.top() + 14
            self.move(max(area.left(), area.right() - self.width() - 13), max(area.top(), top))
        self.show()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self._count = 0
        super().hideEvent(event)

    def _open(self):
        self.hide()
        self.openRequested.emit()
