"""Local controls for reviewing proposed deadlines and managing reminders."""

from datetime import datetime, timezone
from sqlite3 import Error as DatabaseError

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .agent_store import AgentPreferences, DETAIL_OPTIONS, HISTORY_LIMIT, LANGUAGE_OPTIONS


def _label(text=""):
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setText(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _button(text):
    button = QPushButton(text)
    button.setAutoDefault(False)
    return button


class DeadlineCard(QFrame):
    """The proposed date is separate from the user's explicit create action."""

    confirmed = Signal()

    def __init__(self, proposal, store):
        super().__init__()
        self.setObjectName("chatDraftCard")
        self.proposal = proposal
        self.store = store
        column = QVBoxLayout(self)
        column.addWidget(_label("建议待办"))
        source = proposal.source
        column.addWidget(_label(f"《{source.subject}》 · {source.account}\n“{proposal.evidence}”"))
        form = QFormLayout()
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
        self.confirm = _button("确认并添加提醒")
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
        self.title_edit.setReadOnly(True)
        self.due_edit.setReadOnly(True)
        self.feedback.hide()
        self.confirmed.emit()


class TaskPage(QWidget):
    open_mail = Signal(object)
    changed = Signal()

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.scope = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 6)
        layout.setSpacing(10)
        self.show_completed = QCheckBox("显示已完成")
        layout.addWidget(self.show_completed)
        self.list = QListWidget()
        self.list.setObjectName("agentTasks")
        self.list.setAccessibleName("当前邮箱范围的待办")
        layout.addWidget(self.list, 1)
        self.detail = _label()
        self.detail.setObjectName("agentHelp")
        layout.addWidget(self.detail)
        actions = QHBoxLayout()
        self.open_source = _button("打开原邮件")
        self.complete = _button("标记完成")
        actions.addWidget(self.open_source)
        actions.addStretch()
        actions.addWidget(self.complete)
        layout.addLayout(actions)
        self.show_completed.toggled.connect(self.refresh)
        self.list.currentItemChanged.connect(self._selection)
        self.open_source.clicked.connect(self._open)
        self.complete.clicked.connect(self._finish)

    def refresh(self):
        task = self._current()
        self.list.clear()
        try:
            tasks = self.store.list_tasks(self.scope, include_completed=self.show_completed.isChecked())
        except (DatabaseError, OSError) as exc:
            self._selection()
            self.detail.setText(f"读取待办失败：{exc}")
            return
        selected = 0
        for index, entry in enumerate(tasks):
            due = entry.due_at.astimezone().strftime("%Y-%m-%d %H:%M")
            status = "已完成 · " if entry.completed else ""
            row = QListWidgetItem(f"{status}{entry.title}\n{due}  ·  {entry.source.account}")
            row.setData(Qt.ItemDataRole.UserRole, entry)
            self.list.addItem(row)
            if task and task.id == entry.id:
                selected = index
        self.list.setCurrentRow(selected if tasks else -1)
        self._selection()

    def _current(self):
        row = self.list.currentItem()
        return row.data(Qt.ItemDataRole.UserRole) if row else None

    def _selection(self):
        task = self._current()
        self.list.setVisible(bool(task))
        self.detail.setAlignment(Qt.AlignmentFlag.AlignLeft if task else Qt.AlignmentFlag.AlignCenter)
        self.layout().setStretchFactor(self.detail, 0 if task else 1)
        self.complete.setVisible(bool(task))
        self.open_source.setVisible(bool(task))
        self.complete.setEnabled(bool(task and not task.completed))
        self.open_source.setEnabled(bool(task))
        if task:
            self.detail.setText(f"来源：《{task.source.subject}》\n{task.evidence}")
        else:
            self.detail.setText("当前范围暂无待办\n\n在对话中查找截止日期，核对原文后即可添加提醒。")

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


class PreferencePage(QWidget):
    changed = Signal()

    def __init__(self, store):
        super().__init__()
        self.store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 6)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.language = QComboBox()
        self.language.addItems(LANGUAGE_OPTIONS)
        form.addRow("助手回答语言", self.language)
        self.detail = QComboBox()
        self.detail.addItems(DETAIL_OPTIONS)
        form.addRow("回答详略", self.detail)
        self.focus = QLineEdit()
        self.focus.setMaxLength(200)
        self.focus.setPlaceholderText("例如：审稿、会议和需要我确认的邮件")
        form.addRow("优先关注", self.focus)
        self.lead = QSpinBox()
        self.lead.setRange(0, 10080)
        self.lead.setSuffix(" 分钟")
        self.lead.setSpecialValueText("截止时")
        form.addRow("提前提醒", self.lead)
        layout.addLayout(form)
        self.save = _button("保存偏好")
        self.save.clicked.connect(self._save)
        layout.addWidget(self.save, 0, Qt.AlignmentFlag.AlignLeft)
        self.feedback = _label()
        self.feedback.hide()
        layout.addWidget(self.feedback)
        layout.addSpacing(8)
        help_text = _label(
            f"偏好适用于本机所有邮箱。每个查询范围最多记住最近 {HISTORY_LIMIT} 轮问答的节选和邮件来源，"
            "可在对话页清除。待办会在邮件助手运行时提醒。")
        help_text.setObjectName("agentHelp")
        layout.addWidget(help_text)
        layout.addStretch()
        self.load()

    def load(self):
        try:
            prefs = self.store.preferences()
        except (DatabaseError, OSError) as exc:
            self.feedback.setText(f"读取偏好失败：{exc}")
            self.feedback.show()
            return
        self.language.setCurrentText(prefs.language)
        self.detail.setCurrentText(prefs.detail)
        self.focus.setText(prefs.focus)
        self.lead.setValue(prefs.reminder_lead_minutes)

    def _save(self):
        try:
            self.store.set_preferences(AgentPreferences(
                language=self.language.currentText(), detail=self.detail.currentText(),
                reminder_lead_minutes=self.lead.value(), focus=self.focus.text().strip()))
        except (ValueError, DatabaseError, OSError) as exc:
            self.feedback.setText(f"保存偏好失败：{exc}")
            self.feedback.show()
            return
        self.feedback.setText("已保存。")
        self.feedback.show()
        self.changed.emit()
