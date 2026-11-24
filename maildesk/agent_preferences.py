"""The assistant's local answer and deadline notification preferences."""

from sqlite3 import Error as DatabaseError

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QTabBar, QVBoxLayout, QWidget,
)

from .agent_store import AgentPreferences, DETAIL_OPTIONS, HISTORY_LIMIT, LANGUAGE_OPTIONS
from .mail_visuals import icon


_STYLE = """
    QWidget#preferencesPage, QWidget#preferencesContent { background: #f6f7f9; }
    QScrollArea#preferencesScroll { border: none; background: #f6f7f9; }
    QLabel#preferencesTitle { color: #20242b; font-size: 23px; font-weight: 650; }
    QLabel#preferencesSection { color: #404750; font-size: 13px; font-weight: 600; }
    QLabel#preferencesLabel { color: #262c33; font-size: 14px; }
    QLabel#preferencesCaption { color: #626b76; font-size: 12px; }
    QFrame#preferencesGroup { background: white; border: 1px solid #dfe3e9; border-radius: 10px; }
    QFrame#preferencesRule { background: #e9ecf0; border: none; }
    QFrame#preferencesFooter { background: #fbfbfc; border-top: 1px solid #dfe3e9; }
    QLineEdit#preferencesTopics, QComboBox#preferencesMenu, QSpinBox#preferencesMinutes {
        color: #20242b; background: #f8f9fb; border: 1px solid #d5dae2;
        border-radius: 6px; padding: 6px 9px; selection-background-color: #acd0f7; }
    QComboBox#preferencesMenu { padding-right: 25px; }
    QComboBox#preferencesMenu::drop-down { width: 22px; border: none; }
    QComboBox#preferencesMenu::down-arrow { image: none; width: 0; height: 0; }
    QSpinBox#preferencesMinutes::up-button, QSpinBox#preferencesMinutes::down-button { width: 0; height: 0; }
    QSpinBox#preferencesMinutes QLineEdit { color: #20242b; border: none; background: transparent; padding: 0; }
    QLineEdit#preferencesTopics:focus, QComboBox#preferencesMenu:focus, QSpinBox#preferencesMinutes:focus {
        border: 1px solid #4385c7; background: white; }
    QComboBox#preferencesMenu QAbstractItemView { color: #20242b; background: white;
        selection-color: #194a79; selection-background-color: #e2effc; }
    QTabBar#preferencesDetail { background: #edf0f4; border-radius: 6px; }
    QTabBar#preferencesDetail::tab { background: transparent; border: 1px solid transparent;
        border-radius: 5px; color: #49525c; margin: 2px; padding: 2px 12px; min-height: 18px; }
    QTabBar#preferencesDetail::tab:selected { background: #fff; color: #204f78; border: 1px solid #cbd4dd; }
    QTabBar#preferencesDetail:focus::tab:selected { border: 1px solid #3382cd; }
    QPushButton#preferencesSave { color: white; background: #1469c2; border-radius: 7px;
        border: 1px solid #1469c2; padding: 6px 18px; font-weight: 600; }
    QPushButton#preferencesSave:hover { background: #105eae; }
    QPushButton#preferencesSave:focus { border: 1px solid #16416d; }
    QPushButton#preferencesSave:disabled { color: #69717b; background: #e6e9ee; border-color: #e6e9ee; }
"""

_LEAD_OPTIONS = (
    ("截止时", 0), ("提前 15 分钟", 15), ("提前 30 分钟", 30),
    ("提前 1 小时", 60), ("提前 1 天", 1440), ("自定义…", None),
)


def _label(text, name, *, wrap=False):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    return label


def _group(layout, title):
    layout.addWidget(_label(title, "preferencesSection"))
    card = QFrame()
    card.setObjectName("preferencesGroup")
    layout.addWidget(card)
    rows = QVBoxLayout(card)
    rows.setContentsMargins(15, 2, 15, 2)
    rows.setSpacing(0)
    return rows


def _rule(rows):
    line = QFrame()
    line.setObjectName("preferencesRule")
    line.setFixedHeight(1)
    rows.addWidget(line)
    return line


def _row(rows, title, editor):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 8, 0, 8)
    label = _label(title, "preferencesLabel")
    label.setBuddy(editor)
    editor.setAccessibleName(title)
    editor.setMinimumWidth(138)
    layout.addWidget(label)
    layout.addStretch()
    layout.addWidget(editor)
    rows.addWidget(row)
    return row


class _DetailChoice(QTabBar):
    def __init__(self):
        super().__init__()
        self.setObjectName("preferencesDetail")
        self.setDrawBase(False)
        self.setExpanding(True)
        self.setUsesScrollButtons(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for value in DETAIL_OPTIONS:
            self.addTab(value)

    def currentText(self):
        return self.tabText(self.currentIndex())

    def setCurrentText(self, text):
        if text in DETAIL_OPTIONS:
            self.setCurrentIndex(DETAIL_OPTIONS.index(text))


class _PreferenceMenu(QComboBox):
    def __init__(self):
        super().__init__()
        self.setObjectName("preferencesMenu")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        icon("chevron", "#636c76", 13).paint(painter, QRect(self.width() - 22, (self.height() - 13) // 2, 13, 13))


class PreferencePage(QWidget):
    changed = Signal()

    def __init__(self, store):
        super().__init__()
        self.store = store
        self._saved = None
        self.setObjectName("preferencesPage")
        self.setStyleSheet(_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("preferencesScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        content = QWidget()
        content.setObjectName("preferencesContent")
        self.scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 20, 24, 14)
        body.setSpacing(6)
        body.addWidget(_label("助手偏好", "preferencesTitle"))
        body.addWidget(_label("适用于本机所有邮箱", "preferencesCaption"))
        body.addSpacing(9)

        answers = _group(body, "回答方式")
        self.language = _PreferenceMenu()
        self.language.addItems(LANGUAGE_OPTIONS)
        _row(answers, "回答语言", self.language)
        _rule(answers)
        self.detail = _DetailChoice()
        _row(answers, "回答详略", self.detail)
        _rule(answers)
        topics = QVBoxLayout()
        topics.setContentsMargins(0, 8, 0, 10)
        topics.setSpacing(5)
        self.focus = QLineEdit()
        self.focus.setObjectName("preferencesTopics")
        self.focus.setMaxLength(200)
        self.focus.setAccessibleName("优先关注")
        self.focus.setPlaceholderText("例如审稿进度、需要确认时间的会议")
        focus_label = _label("优先关注", "preferencesLabel")
        focus_label.setBuddy(self.focus)
        topics.addWidget(focus_label)
        topics.addWidget(self.focus)
        answers.addLayout(topics)
        body.addWidget(_label(f"每个邮箱范围保留最近 {HISTORY_LIMIT} 轮问答，可在「对话」中清除。",
                              "preferencesCaption", wrap=True))
        body.addSpacing(8)

        reminders = _group(body, "截止提醒")
        self.lead_choice = _PreferenceMenu()
        for label, minutes in _LEAD_OPTIONS:
            self.lead_choice.addItem(label, minutes)
        _row(reminders, "提醒时间", self.lead_choice)
        self._custom_rule = _rule(reminders)
        self.lead = QSpinBox()
        self.lead.setObjectName("preferencesMinutes")
        self.lead.lineEdit().setTextMargins(8, 0, 8, 0)
        self.lead.setRange(0, 10080)
        self.lead.setSuffix(" 分钟")
        self.lead.setSpecialValueText("截止时")
        self._custom_row = _row(reminders, "提前分钟数", self.lead)
        body.addWidget(_label("应用运行时弹窗。退出期间错过的提醒会在下次启动时补发。",
                              "preferencesCaption", wrap=True))
        body.addStretch()

        footer = QFrame()
        footer.setObjectName("preferencesFooter")
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(24, 9, 24, 9)
        self.feedback = _label("", "preferencesFeedback", wrap=True)
        self.feedback.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        actions.addWidget(self.feedback, 1)
        self.save = QPushButton("保存偏好")
        self.save.setAutoDefault(False)
        self.save.setObjectName("preferencesSave")
        actions.addWidget(self.save)
        outer.addWidget(footer)

        self.language.currentIndexChanged.connect(self._edited)
        self.detail.currentChanged.connect(self._edited)
        self.focus.textChanged.connect(self._edited)
        self.lead_choice.currentIndexChanged.connect(self._choose_lead)
        self.lead_choice.activated.connect(self._focus_custom_lead)
        self.lead.valueChanged.connect(self._lead_changed)
        self.save.clicked.connect(self._save)
        self.load()

    def _choose_lead(self):
        minutes = self.lead_choice.currentData()
        self._custom_row.setVisible(minutes is None)
        self._custom_rule.setVisible(minutes is None)
        if minutes is not None:
            self.lead.setValue(minutes)

    def _lead_changed(self, minutes):
        if self.lead_choice.currentData() not in (minutes, None):
            self._match_lead(minutes)
        self._edited()

    def _focus_custom_lead(self):
        if self.lead_choice.currentData() is None:
            self.lead.setFocus()
            self.lead.selectAll()
            QTimer.singleShot(0, self, lambda: self.scroll.ensureWidgetVisible(self.lead))

    def _match_lead(self, minutes):
        index = self.lead_choice.findData(minutes)
        self.lead_choice.setCurrentIndex(index if index >= 0 else self.lead_choice.count() - 1)
        self._choose_lead()

    def _values(self):
        return AgentPreferences(language=self.language.currentText(), detail=self.detail.currentText(),
                                reminder_lead_minutes=self.lead.value(), focus=self.focus.text().strip())

    def _status(self, message, color):
        self.feedback.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.feedback.setText(message)

    def _edited(self):
        if self._saved is None:
            return
        dirty = self._values() != self._saved
        self.save.setEnabled(dirty)
        self._status("有未保存的更改" if dirty else "当前设置已生效", "#835714" if dirty else "#626b76")

    def load(self):
        try:
            prefs = self.store.preferences()
        except (DatabaseError, OSError, ValueError) as exc:
            self._saved = None
            self.scroll.setEnabled(False)
            self.save.setEnabled(True)
            self.save.setText("重新读取")
            self._status(f"读取偏好失败：{exc}", "#a33430")
            return
        self._saved = None
        self.language.setCurrentText(prefs.language)
        self.detail.setCurrentText(prefs.detail)
        self.focus.setText(prefs.focus)
        self.lead.setValue(prefs.reminder_lead_minutes)
        self._match_lead(prefs.reminder_lead_minutes)
        self._saved = prefs
        self.scroll.setEnabled(True)
        self.save.setText("保存偏好")
        self._edited()

    def _save(self):
        if self._saved is None:
            self.load()
            return
        try:
            values = self._values()
            self.store.set_preferences(values)
        except (DatabaseError, OSError, ValueError) as exc:
            self._status(f"保存失败：{exc}", "#a33430")
            return
        self._saved = values
        self.focus.setText(values.focus)
        self.save.setEnabled(False)
        self._status("已保存，设置已生效", "#256644")
        self.changed.emit()
