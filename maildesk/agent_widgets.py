"""Assistant pages and the inline form for confirming a suggested deadline."""

from datetime import datetime, timezone
from sqlite3 import Error as DatabaseError

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QDateTimeEdit, QFormLayout, QFrame, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout,
)

from .agent_preferences import PreferencePage
from .agent_tasks import TaskPage
from .mail_visuals import icon

__all__ = ["DeadlineCard", "PreferencePage", "TaskPage"]


def _label(text=""):
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setText(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class DeadlineCard(QFrame):
    """The proposed date is separate from the user's explicit create action."""

    confirmed = Signal()

    def __init__(self, proposal, store):
        super().__init__()
        self.setObjectName("deadlineProposal")
        self.proposal = proposal
        self.store = store
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(8)
        heading = _label("确认截止待办")
        heading.setObjectName("deadlineHeading")
        column.addWidget(heading)
        source = proposal.source
        citation = _label(proposal.evidence)
        citation.setObjectName("deadlineQuote")
        column.addWidget(citation)
        origin = _label(f"《{source.subject}》 · {source.account}")
        origin.setObjectName("chatRole")
        column.addWidget(origin)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.title_edit = QLineEdit(proposal.title)
        self.title_edit.setMaxLength(200)
        form.addRow("事项", self.title_edit)
        self.due_edit = QDateTimeEdit(QDateTime.fromSecsSinceEpoch(int(proposal.due_at.timestamp())))
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("截止（本地时间）", self.due_edit)
        column.addLayout(form)
        if proposal.date_only:
            column.addWidget(_label("原邮件只写了日期。已按该日期的 23:59 换算为本地时间，可调整。"))
        self.confirm = QPushButton("确认并添加提醒")
        self.confirm.setAutoDefault(False)
        self.confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm.setObjectName("confirmDeadline")
        self.confirm.clicked.connect(self._add)
        self.feedback = _label()
        self.feedback.hide()
        column.addWidget(self.confirm, 0, Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self.feedback)

    def _add(self):
        title = self.title_edit.text().strip()
        if not title:
            self.feedback.setText("请填写待办事项。")
            self.feedback.show()
            return
        due = datetime.fromtimestamp(self.due_edit.dateTime().toSecsSinceEpoch(), timezone.utc)
        try:
            task = self.store.add_task(title, due, self.proposal.source, self.proposal.evidence)
        except (ValueError, DatabaseError, OSError) as exc:
            self.feedback.setText(f"保存提醒失败：{exc}")
            self.feedback.show()
            return
        self.confirm.setEnabled(False)
        self.confirm.setText("已完成" if task.completed else "已加入待办")
        self.confirm.setIcon(icon("checklist", "#537865", 14))
        self.title_edit.setReadOnly(True)
        self.due_edit.setReadOnly(True)
        self.feedback.hide()
        self.confirmed.emit()
