"""Scoped mailbox conversations, source-backed reminders and local preferences."""

from concurrent.futures import CancelledError
from sqlite3 import Error as DatabaseError
from threading import Event

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .mail_visuals import icon, sender_display


def _label(text, *, name="", selectable=False):
    label = QLabel()
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                     Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setText(text)
    return label


def _button(title, name):
    button = QPushButton(title)
    button.setObjectName(name)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class _QuestionEdit(QPlainTextEdit):
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self._composing = False
        self.setTabChangesFocus(True)
        self.setFixedHeight(62)
        self.setAccessibleName("向邮箱助手提问")
        self.setPlaceholderText("问问你的邮箱…")
        self.setToolTip("回车提问，Shift + 回车换行")

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if (not self._composing and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _SourceButton(QPushButton):
    def __init__(self, number, item):
        super().__init__()
        self.setObjectName("chatSource")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        subject = " ".join(item.subject.split()) or "（无主题）"
        self._heading = f"[{number}]   {subject}"
        self._detail = "  ·  ".join(filter(None, (sender_display(item.sender), item.account, item.date)))
        self.setAccessibleName(f"打开原邮件 {self._heading}，{self._detail}")
        self.setToolTip(self.accessibleName())
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(50)
        self._fit_text()

    def _fit_text(self):
        metrics = self.fontMetrics()
        lines = [metrics.elidedText(line, Qt.TextElideMode.ElideRight, max(60, self.width() - 30))
                 for line in (self._heading, self._detail)]
        self.setText("\n".join(lines).replace("&", "&&"))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_text()


class ChatDialog(QDialog):
    open_mail = Signal(object)
    compose_reply = Signal(object, str)
    draft_ready = Signal(object, str)

    tasks_changed = Signal()

    def __init__(self, parent=None, *, accounts, model_description, ask, run, store=None):
        super().__init__(parent)
        self.setObjectName("chatDialog")
        self.setWindowTitle("邮箱助手")
        self.resize(650, 660)
        self.setMinimumSize(390, 420)
        self._store = store
        self.pages = None
        self._run = run
        self._ask = ask
        self._conversation_id = 0
        self._pending = None
        self._question_row = None
        self._active_question = ""
        self._destroyed = Event()
        self.destroyed.connect(self._destroyed.set)
        self.history = []
        self.setStyleSheet("""
            QDialog#chatDialog, QWidget#chatTranscript, QWidget#chatWelcome { background: white; color: #252529; }
            QLabel#chatWelcomeTitle { color: #26262b; font-size: 22px; font-weight: bold; }
            QLabel#chatModel, QLabel#chatRole, QLabel#chatScopeLabel { color: #686872; font-size: 12px; }
            QLabel#chatQuestion { color: #184471; background: #edf4fd; padding: 9px 13px; border-radius: 11px; }
            QLabel#chatAnswer, QLabel#chatDraftText { color: #26262b; font-size: 14px; }
            QFrame#chatDraftCard { background: #f8fafc; border: 1px solid #dfe5ec; border-radius: 8px; }
            QFrame#chatRule { background: #e6e6eb; }
            QScrollArea#chatScroll, QPlainTextEdit#chatInput { border: none; background: white; color: #26262b; }
            QFrame#chatComposer { border: 1px solid #d4d8df; border-radius: 12px; background: white; }
            QPushButton#chatSource, QPushButton#chatSuggestion {
                text-align: left; border: 1px solid #e2e5e9; border-radius: 7px;
                color: #295379; background: #f8fafb; padding: 6px 12px;
            }
            QPushButton#chatSource:hover, QPushButton#chatSuggestion:hover { background: #edf4fc; }
            QPushButton#chatSubmit:enabled { background: #087af2; color: white; border: 0; border-radius: 8px; padding: 6px 14px; }
            QPushButton#chatReset, QPushButton#chatCompose { background: #edf3fa; color: #1767a9; border: 0; border-radius: 7px; padding: 6px 10px; }
            QLabel#chatStatus, QLabel#agentHelp { color: #646470; }
            QTabWidget::pane { border: none; border-top: 1px solid #e6e6eb; }
            QTabBar::tab { padding: 7px 17px; color: #616875; background: white; }
            QTabBar::tab:selected { color: #1767a9; border-bottom: 2px solid #337fc0; }
            QTabBar::tab:hover { background: #f0f5fa; }
            QListWidget#agentTasks { border: 1px solid #e2e5e9; border-radius: 6px; background: white; }
            QListWidget#agentTasks::item { padding: 7px 8px; }
            QListWidget#agentTasks::item:selected { background: #edf4fd; color: #184471; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 14)
        layout.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(18, 13, 18, 10)
        self.scope = QComboBox()
        self.scope.setAccessibleName("查询邮箱范围")
        self.scope.setToolTip("切换此范围的对话和待办")
        self.scope.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        scope_label = QLabel("查询范围")
        scope_label.setObjectName("chatScopeLabel")
        scope_label.setBuddy(self.scope)
        top.addWidget(scope_label)
        top.addWidget(self.scope, 1)
        self.new_chat = _button("清除对话" if store is not None else "新对话", "chatReset")
        self.new_chat.setToolTip("清除当前查询范围的对话记忆")
        top.addWidget(self.new_chat)
        layout.addLayout(top)
        line = QFrame()
        line.setObjectName("chatRule")
        line.setFixedHeight(1)
        layout.addWidget(line)

        if store is not None:
            from .agent_widgets import PreferencePage, TaskPage

            self.pages = QTabWidget()
            self.pages.setDocumentMode(True)
            layout.addWidget(self.pages, 1)
            conversation = QWidget()
            self.pages.addTab(conversation, "对话")
            self.tasks = TaskPage(store)
            self.tasks.open_mail.connect(self.open_mail)
            self.tasks.changed.connect(self.tasks_changed)
            self.pages.addTab(self.tasks, "待办")
            self.prefs = PreferencePage(store)
            self.prefs.changed.connect(self.tasks_changed)
            self.pages.addTab(self.prefs, "偏好")
            self.pages.currentChanged.connect(self._page_changed)
            layout = QVBoxLayout(conversation)
            layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        welcome = QWidget()
        welcome.setObjectName("chatWelcome")
        intro = QVBoxLayout(welcome)
        intro.setContentsMargins(26, 12, 26, 12)
        intro.setSpacing(10)
        intro.addStretch()
        mark = QLabel()
        mark.setPixmap(icon("envelope", "#3675ac", 35).pixmap(35, 35))
        intro.addWidget(mark, 0, Qt.AlignmentFlag.AlignCenter)
        heading = QLabel("和你的邮箱聊聊")
        heading.setObjectName("chatWelcomeTitle")
        intro.addWidget(heading, 0, Qt.AlignmentFlag.AlignCenter)
        intro.addSpacing(8)
        self.suggestions = []
        questions = ["最近有哪些邮件需要回复？", "总结最近一周的来信"]
        if store is not None:
            questions.append("找出最近一周邮件中的截止日期，帮我整理成待办")
        for question in questions:
            suggestion = _button(question, "chatSuggestion")
            suggestion.clicked.connect(lambda checked=False, q=question: self._send(q))
            intro.addWidget(suggestion, 0, Qt.AlignmentFlag.AlignCenter)
            self.suggestions.append(suggestion)
        intro.addStretch()
        self.stack.addWidget(welcome)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("chatTranscript")
        self.messages = QVBoxLayout(content)
        self.messages.setContentsMargins(20, 20, 20, 16)
        self.messages.setSpacing(13)
        self.messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(content)
        self.stack.addWidget(self.scroll)

        bottom = QVBoxLayout()
        bottom.setContentsMargins(16, 5, 16, 0)
        bottom.setSpacing(6)
        self.status = _label("", name="chatStatus")
        self.status.hide()
        bottom.addWidget(self.status)
        composer = QFrame()
        composer.setObjectName("chatComposer")
        box = QVBoxLayout(composer)
        box.setContentsMargins(8, 6, 8, 7)
        box.setSpacing(1)
        self.input = _QuestionEdit()
        self.input.setObjectName("chatInput")
        box.addWidget(self.input)
        actions = QHBoxLayout()
        self.model_label = _label("", name="chatModel")
        actions.addWidget(self.model_label, 1)
        self.send_button = _button("提问", "chatSubmit")
        actions.addWidget(self.send_button)
        box.addLayout(actions)
        bottom.addWidget(composer)
        layout.addLayout(bottom)

        self.new_chat.clicked.connect(self._clear_history)
        self.scope.currentIndexChanged.connect(self.reset)
        self.input.textChanged.connect(self._update_controls)
        self.input.submitted.connect(self._submit)
        self.send_button.clicked.connect(self._click_submit)
        self.reconfigure(accounts=accounts, model_description=model_description, ask=ask)
        self.input.setFocus()

    def reconfigure(self, *, accounts, model_description, ask):
        previous = self.scope.currentData()
        self.scope.blockSignals(True)
        self._accounts = tuple(accounts)
        self.scope.clear()
        self.scope.addItem("全部收件箱", "")
        for account in self._accounts:
            self.scope.addItem(account.address, account.address)
        self.scope.setCurrentIndex(max(0, self.scope.findData(previous)))
        self.scope.blockSignals(False)
        self._ask = ask
        self.model_label.setText(f"提问时，问题、偏好和相关邮件节选将交给{model_description}。"
                                 if ask else "启用 AI 后可在这里查信、总结和提取待办。")
        self.reset()

    def _scope_addresses(self):
        selected = self.scope.currentData()
        return tuple(a.address for a in self._accounts if not selected or a.address == selected)

    def show_tasks(self, account=None):
        if self.pages is not None:
            if account is not None:
                index = self.scope.findData(account)
                if index >= 0:
                    self.scope.setCurrentIndex(index)
            if self.pages.currentWidget() is self.tasks:
                self.tasks.refresh()
            else:
                self.pages.setCurrentWidget(self.tasks)

    def show_conversation(self):
        if self.pages is not None:
            self.pages.setCurrentIndex(0)

    def _page_changed(self, index):
        self.new_chat.setVisible(index == 0)
        self.scope.setEnabled(self.pages.currentWidget() is not self.prefs)
        if self.pages.currentWidget() is self.tasks:
            self.tasks.refresh()

    def _clear_history(self):
        if self._store is not None:
            try:
                self._store.clear_history(self._scope_addresses())
            except (DatabaseError, OSError) as exc:
                self._set_status(f"清除对话失败：{exc}")
                return
        self.reset()

    @property
    def conversation_id(self):
        return self._conversation_id

    def show_error(self, message):
        if self.pages is not None and self.pages.currentWidget() is self.tasks:
            self.tasks.detail.setText(str(message))
        else:
            self._set_status(str(message))

    def disable(self):
        self._ask = None
        self.reset(restore=False)
        self.close()

    def reset(self, *, restore=True):
        self._conversation_id += 1
        self._cancel()
        self.history.clear()
        while self.messages.count():
            widget = self.messages.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.input.clear()
        self.stack.setCurrentIndex(0)
        self._set_status("请先绑定邮箱。" if not self._accounts else "")
        if self._store is not None:
            scope = self._scope_addresses()
            self.tasks.scope = scope
            if self.pages.currentWidget() is self.tasks:
                self.tasks.refresh()
            if scope and restore:
                try:
                    self.history.extend(self._store.history(scope))
                except (DatabaseError, OSError) as exc:
                    self._set_status(f"读取对话记忆失败：{exc}")
                for turn in self.history:
                    self._add_question(turn.question)
                    self._add_answer(turn)
                self.stack.setCurrentIndex(int(bool(self.history)))
        self._update_controls()

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _update_controls(self):
        available = bool(self._ask and self._accounts)
        busy = self._pending is not None
        self.input.setEnabled(available)
        self.send_button.setEnabled(available and (busy or bool(self.input.toPlainText().strip())))
        self.send_button.setText("停止" if busy else "提问")
        self.new_chat.setEnabled(bool(self.history or busy or self.input.toPlainText()))
        for suggestion in self.suggestions:
            suggestion.setEnabled(available and not busy)

    def _click_submit(self):
        if self._pending is not None:
            self._cancel()
            self._set_status("已停止。")
        else:
            self._submit()

    def _submit(self):
        self._send(self.input.toPlainText())

    def _cancel(self):
        if self._pending is None:
            return
        self._pending.set()
        self._pending = None
        if self._question_row is not None:
            self.messages.removeWidget(self._question_row)
            self._question_row.hide()
            self._question_row.deleteLater()
            self._question_row = None
        if not self.input.toPlainText().strip():
            self.input.setPlainText(self._active_question)
        self._active_question = ""
        self.stack.setCurrentIndex(1 if self.history else 0)
        self._update_controls()

    def _send(self, question):
        question = question.strip()
        if self._pending is not None or not question or self._ask is None or not self._accounts:
            return
        if len(question) > 2000:
            self._set_status("请将问题缩短至 2000 字以内。")
            return
        address = self.scope.currentData()
        accounts = tuple(a for a in self._accounts if not address or a.address == address)
        ask, history = self._ask, tuple(self.history)
        stop = Event()
        lifetime = self._destroyed

        def cancelled():
            return stop.is_set() or lifetime.is_set()

        self._pending = stop
        self._active_question = question
        self.input.clear()
        self.stack.setCurrentIndex(1)
        self._question_row = self._add_question(question)
        self._set_status("正在查找邮件并整理回答…")
        self._update_controls()

        def work():
            if cancelled():
                raise CancelledError()
            return ask(question, accounts, history, cancelled=cancelled)

        def finished(turn, error):
            if cancelled() or not isValid(self):
                return
            if error:
                self._cancel()
                self._set_status("已停止。" if isinstance(error, CancelledError) else str(error))
                return
            self._pending = None
            self._active_question = ""
            self._question_row = None
            self.history.append(turn)
            self._add_answer(turn)
            self._set_status("")
            if self._store is not None:
                from .agent_store import HISTORY_LIMIT

                try:
                    self._store.save_turn(self._scope_addresses(), turn)
                except (ValueError, DatabaseError, OSError) as exc:
                    self._set_status(f"本轮对话仅保留在窗口中，保存失败：{exc}")
                if len(self.history) > HISTORY_LIMIT:
                    self.history.pop(0)
                    for _ in range(2):
                        widget = self.messages.takeAt(0).widget()
                        widget.hide()
                        widget.deleteLater()
            self._update_controls()
            if turn.draft_record is not None:
                item = turn.draft_record.item
                prior = next((t.draft_text for t in reversed(history) if t.draft_item and
                              (t.draft_item.account, t.draft_item.validity, t.draft_item.uid)
                              == (item.account, item.validity, item.uid)), "")
                self.draft_ready.emit(turn.draft_record, prior)

        self._run(work, finished)

    def _add_question(self, text):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(45, 0, 0, 0)
        layout.addStretch(1)
        bubble = _label(text, name="chatQuestion", selectable=True)
        bubble.setAccessibleName("你的问题")
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout.addWidget(bubble)
        self.messages.addWidget(row)
        self._scroll_to(row)
        return row

    def _add_answer(self, turn):
        row = QWidget()
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 8, 3)
        column.setSpacing(6)
        tool_names = {"search_mail": "检索邮件", "read_mail": "读取原文",
                      "list_tasks": "查看待办", "propose_deadline": "提取截止日期",
                      "prepare_draft": "起草回复"}
        actions = dict.fromkeys(tool_names[key] for key in turn.tools_used if key in tool_names)
        role = "邮箱助手" + ("  ·  " + "、".join(actions) if actions else "")
        column.addWidget(_label(role, name="chatRole"))
        column.addWidget(_label(turn.answer, name="chatAnswer", selectable=True))
        for number, item in enumerate(turn.sources, 1):
            button = _SourceButton(number, item)
            button.clicked.connect(lambda checked=False, mail=item: self.open_mail.emit(mail))
            column.addWidget(button)
        if self._store is not None and turn.deadline_proposals:
            from .agent_widgets import DeadlineCard

            for proposal in turn.deadline_proposals:
                if proposal.source.account in self._scope_addresses():
                    card = DeadlineCard(proposal, self._store)
                    card.confirmed.connect(self.tasks_changed)
                    column.addWidget(card)
        if turn.draft_item is not None:
            card = QFrame()
            card.setObjectName("chatDraftCard")
            draft = QVBoxLayout(card)
            draft.setContentsMargins(12, 10, 12, 10)
            draft.addWidget(_label("回复草稿", name="chatRole"))
            draft.addWidget(_label(turn.draft_text, name="chatDraftText", selectable=True))
            edit = _button("在回复窗口编辑", "chatCompose")
            edit.clicked.connect(lambda: self.compose_reply.emit(turn.draft_item, turn.draft_text))
            draft.addWidget(edit, 0, Qt.AlignmentFlag.AlignLeft)
            column.addWidget(card)
        self.messages.addWidget(row)
        self._scroll_to(row)

    def _scroll_to(self, row):
        def scroll():
            if isValid(self) and isValid(row) and row.isVisibleTo(self.scroll):
                self.scroll.verticalScrollBar().setValue(row.y())
        QTimer.singleShot(0, scroll)

    def reject(self):
        self._conversation_id += 1
        self._cancel()
        self._set_status("")
        super().reject()
