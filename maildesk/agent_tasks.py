"""Deadline list and source inspector for the mailbox assistant."""

from bisect import bisect_left
from datetime import datetime
from sqlite3 import Error as DatabaseError

from PySide6.QtCore import QSignalBlocker, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from .mail_visuals import icon


def _deadline_label(task, now):
    due = task.due_at.astimezone()
    if task.completed:
        return "已完成", "#637166"
    if task.due_at < now:
        return "已逾期", "#a93d32"
    days = (due.date() - now.date()).days
    if days == 0:
        return "今天", "#1469c2"
    if days == 1:
        return "明天", "#335e88"
    return "", "#596676"


class _TaskDelegate(QStyledItemDelegate):
    """Draw visible rows without creating a widget for each deadline."""

    def sizeHint(self, option, index):
        return QSize(180, 62)

    def paint(self, painter, option, index):
        task = index.data(Qt.ItemDataRole.UserRole)
        if task is None:
            super().paint(painter, option, index)
            return
        painter.save()
        try:
            self._draw(painter, option, task)
        finally:
            painter.restore()

    @staticmethod
    def _draw(painter, option, task):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(3, 2, -3, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#eaf2fd" if selected else "#f4f6f8"))
            painter.drawRoundedRect(QRectF(rect), 7, 7)

        now = datetime.now().astimezone()
        due = task.due_at.astimezone()
        state, ink = _deadline_label(task, now)
        calendar = QRect(rect.left() + 9, rect.center().y() - 20, 36, 40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff" if selected else "#f1f4f7"))
        painter.drawRoundedRect(QRectF(calendar), 6, 6)

        font = QFont(option.font)
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor("#586574"))
        painter.drawText(calendar.adjusted(0, 1, 0, -21), Qt.AlignmentFlag.AlignCenter, f"{due.month}月")
        font.setPixelSize(17)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#546274" if task.completed else "#28384a"))
        painter.drawText(calendar.adjusted(0, 15, 0, -1), Qt.AlignmentFlag.AlignCenter, str(due.day))

        right = rect.right() - 10
        left = calendar.right() + 12
        status = state if due.year == now.year else " · ".join(filter(None, (state, str(due.year))))
        time = due.strftime("%H:%M")
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.Normal)
        small = QFontMetrics(font)
        time_width = max(small.horizontalAdvance(status), small.horizontalAdvance(time)) + 4
        text_width = max(0, right - time_width - 12 - left)
        mid = rect.center().y()
        first_line = QRect(left, mid - 20, text_width, 24)
        second_line = QRect(left, mid + 4, text_width, 18)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        trailing = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        painter.setFont(font)
        painter.setPen(QColor("#586574"))
        painter.drawText(second_line, align, small.elidedText(task.source.account, Qt.TextElideMode.ElideRight, text_width))
        painter.setPen(QColor(ink))
        painter.drawText(QRect(right - time_width, mid - 18, time_width, 20), trailing, time)
        painter.drawText(QRect(right - time_width, mid + 3, time_width, 18), trailing, status)

        font.setPixelSize(14)
        font.setWeight(QFont.Weight.DemiBold)
        font.setStrikeOut(task.completed)
        painter.setFont(font)
        painter.setPen(QColor("#657168" if task.completed else "#1c2735"))
        title = QFontMetrics(font).elidedText(task.title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(first_line, align, title)


class TaskPage(QWidget):
    open_mail = Signal(object)
    changed = Signal()
    request_conversation = Signal()

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.scope = ()
        self._chat_available = False
        self._pending_deadlines = ()
        self.setObjectName("agentTaskPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("""
            QWidget#agentTaskPage { background: #f6f7f9; color: #202733; }
            QLabel#taskTitle { color: #1c2533; font-size: 25px; font-weight: 650; }
            QLabel#taskSummary, QLabel#taskEmptyHelp { color: #606b78; font-size: 13px; }
            QLabel#taskEmptyTitle { color: #273140; font-size: 17px; font-weight: 600; }
            QFrame#taskPaper, QFrame#taskEmpty, QListWidget#taskList {
                background: white; border: 1px solid #dce1e7; border-radius: 10px;
            }
            QListWidget#taskList { padding: 3px; }
            QListWidget#taskList:focus { border-color: #5890c9; }
            QLabel#taskEvidence { color: #384453; font-size: 13px; }
            QLabel#taskSection { color: #606b78; font-size: 12px; font-weight: 600; }
            QLabel#taskEmptyIcon { background: #eaf2fc; border-radius: 12px; }
            QPushButton { padding: 5px 11px; border-radius: 6px; font-size: 13px; }
            QPushButton#taskSource { background: white; border: 1px solid #cfd6df; color: #25425f; }
            QPushButton#taskSource:hover { background: #edf4fc; }
            QPushButton#taskFilter { background: white; border: 1px solid #d6dce3; color: #485464; font-size: 12px; }
            QPushButton#taskFilter:checked { background: #e6f0fb; border-color: #99badf; color: #175e9f; }
            QPushButton#taskPrimary { background: #1469c2; border: 1px solid #1469c2; color: white; font-weight: 550; }
            QPushButton#taskPrimary:hover { background: #095ba9; }
            QPushButton#taskPrimary:disabled { color: #717986; background: #edf0f3; border-color: #e0e4e8; }
            QPushButton:focus { border: 2px solid #368ce1; padding: 4px 10px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 17, 22, 16)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("截止待办")
        title.setObjectName("taskTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.show_completed = QPushButton("显示已完成")
        self.show_completed.setObjectName("taskFilter")
        self.show_completed.setCheckable(True)
        self.show_completed.setAutoDefault(False)
        self.show_completed.setIcon(icon("filter", "#546478", 14))
        self.show_completed.setAccessibleName("显示已完成的待办")
        heading.addWidget(self.show_completed)
        layout.addLayout(heading)
        self.summary = QLabel()
        self.summary.setObjectName("taskSummary")
        layout.addWidget(self.summary)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.list = QListWidget()
        self.list.setObjectName("taskList")
        self.list.setAccessibleName("按截止时间排列的待办")
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_TaskDelegate(self.list))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMinimumSize(0, 45)
        self.pages.addWidget(self.list)

        empty = QFrame()
        empty.setObjectName("taskEmpty")
        empty_box = QVBoxLayout(empty)
        empty_box.setContentsMargins(10, 12, 10, 12)
        empty_box.setSpacing(7)
        empty_box.addStretch()
        mark = QLabel()
        mark.setObjectName("taskEmptyIcon")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(48, 48)
        mark.setPixmap(icon("checklist", "#3677b8", 29).pixmap(29, 29))
        empty_box.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = QLabel()
        self.empty_title.setObjectName("taskEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.addWidget(self.empty_title)
        self.empty_help = QLabel()
        self.empty_help.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_help.setWordWrap(True)
        self.empty_help.setObjectName("taskEmptyHelp")
        self.empty_help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.addWidget(self.empty_help)
        self.empty_action = QPushButton("从邮件查找截止日期")
        self.empty_action.setObjectName("taskPrimary")
        self.empty_action.setAutoDefault(False)
        empty_box.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_box.addStretch()
        self.pages.addWidget(empty)
        self.pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        self.inspector = QFrame()
        self.inspector.setObjectName("taskPaper")
        inspector = QVBoxLayout(self.inspector)
        inspector.setContentsMargins(13, 9, 13, 9)
        inspector.setSpacing(5)
        label = QLabel("邮件依据")
        label.setObjectName("taskSection")
        inspector.addWidget(label)
        self.detail = QLabel()
        self.detail.setObjectName("taskEvidence")
        self.detail.setAccessibleName("来源邮件及截止日期原文")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                           Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.excerpt = QScrollArea()
        self.excerpt.setWidgetResizable(True)
        self.excerpt.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.excerpt.setFrameShape(QFrame.Shape.NoFrame)
        self.excerpt.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: white; }")
        self.excerpt.setWidget(self.detail)
        self.excerpt.setMinimumHeight(34)
        inspector.addWidget(self.excerpt)
        actions = QHBoxLayout()
        self.open_source = QPushButton("打开原邮件")
        self.open_source.setObjectName("taskSource")
        self.open_source.setIcon(icon("envelope", "#315878", 15))
        self.open_source.setAutoDefault(False)
        self.complete = QPushButton("标记完成")
        self.complete.setObjectName("taskPrimary")
        self.complete.setAutoDefault(False)
        actions.addWidget(self.open_source)
        actions.addStretch()
        actions.addWidget(self.complete)
        inspector.addLayout(actions)
        layout.addWidget(self.inspector)

        self._error = ""
        self.show_completed.toggled.connect(self.refresh)
        self.list.currentItemChanged.connect(self._selection)
        self.empty_action.clicked.connect(self._empty_clicked)
        self.open_source.clicked.connect(self._open)
        self.complete.clicked.connect(self._finish)
        self._clock = QTimer(self)
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self._update_dates)
        self._selection()

    def showEvent(self, event):
        super().showEvent(event)
        self._clock.start()

    def hideEvent(self, event):
        self._clock.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.height() < 380
        self.layout().setContentsMargins(19 if compact else 22, 11 if compact else 17,
                                         19 if compact else 22, 9 if compact else 16)
        self.layout().setSpacing(6 if compact else 10)
        self._size_excerpt()

    def _size_excerpt(self):
        available = max(100, self.width() - 100)
        maximum = 37 if self.height() < 380 else 78
        self.excerpt.setMaximumHeight(max(34, min(maximum, self.detail.heightForWidth(available))))

    def set_chat_available(self, available):
        self._chat_available = bool(available)
        if not self.list.count():
            self._show_empty()

    def refresh(self):
        checked = self.show_completed.isChecked()
        self.show_completed.setIcon(icon("checklist" if checked else "filter", "#265f96" if checked else "#546478", 14))
        current = self._current()
        self._error = ""
        try:
            tasks = self.store.list_tasks(self.scope, include_completed=checked)
        except (DatabaseError, OSError) as exc:
            self._error = str(exc)
            tasks = ()
        # The store orders unfinished tasks by deadline. A minute tick need only
        # find the boundary that has passed, not rebuild the list or read SQLite.
        self._pending_deadlines = tuple(entry.due_at for entry in tasks if not entry.completed)
        with QSignalBlocker(self.list):
            self.list.clear()
            selected = 0
            for index, entry in enumerate(tasks):
                row = QListWidgetItem(entry.title)
                row.setData(Qt.ItemDataRole.UserRole, entry)
                deadline = entry.due_at.astimezone().strftime("%Y-%m-%d %H:%M")
                description = f"{entry.title}，截止于 {deadline}，{entry.source.account}"
                if entry.completed:
                    description += "，已完成"
                row.setToolTip(description)
                row.setData(Qt.ItemDataRole.AccessibleTextRole, description)
                self.list.addItem(row)
                if current and current.id == entry.id:
                    selected = index
            self.list.setCurrentRow(selected if tasks else -1)
        self._selection()
        self._update_dates()

    def _update_dates(self):
        now = datetime.now().astimezone()
        pending = len(self._pending_deadlines)
        complete = self.list.count() - pending
        overdue = bisect_left(self._pending_deadlines, now)
        parts = [f"{pending} 项待处理"]
        if overdue:
            parts.append(f"{overdue} 项已逾期")
        if complete:
            parts.append(f"{complete} 项已完成")
        self.summary.setText("  ·  ".join(parts))
        self.summary.setVisible(bool(self.list.count()))
        self.list.viewport().update()

    def _current(self):
        row = self.list.currentItem()
        return row.data(Qt.ItemDataRole.UserRole) if row else None

    def _show_empty(self):
        failed = bool(self._error)
        title = "暂无待办" if self.show_completed.isChecked() else "暂无未完成待办"
        self.empty_title.setText("无法读取待办" if failed else title)
        self.empty_help.setText(self._error or ("在对话中确认截止时间，即可收到提醒。" if self._chat_available
                                               else "启用邮箱 AI 后，可以从来信中提取截止日期。"))
        self.empty_action.setText("重新读取" if failed else "从邮件查找截止日期")
        self.empty_action.setVisible(failed or self._chat_available)

    def _selection(self):
        task = self._current()
        self.pages.setCurrentIndex(0 if task else 1)
        self.inspector.setVisible(bool(task))
        self.summary.setVisible(bool(self.list.count()))
        self.complete.setEnabled(bool(task and not task.completed))
        if task:
            self.complete.setText("已完成" if task.completed else "标记完成")
            self.detail.setText(f"《{task.source.subject or '无主题'}》\n{task.evidence}".rstrip())
            self._size_excerpt()
            self.excerpt.verticalScrollBar().setValue(0)
        else:
            self._show_empty()

    def _empty_clicked(self):
        if self._error:
            self.refresh()
        elif self._chat_available:
            self.request_conversation.emit()

    def _open(self):
        task = self._current()
        if task:
            self.open_mail.emit(task.source)

    def _finish(self):
        task = self._current()
        if task and not task.completed:
            try:
                self.store.complete_task(task.id, self.scope)
            except (DatabaseError, OSError) as exc:
                self.detail.setText(f"更新待办失败：{exc}")
                return
            self.refresh()
            self.changed.emit()
