"""Scoped mailbox conversations, source-backed reminders and local preferences."""

from collections import OrderedDict
from concurrent.futures import CancelledError
from dataclasses import replace
from sqlite3 import Error as DatabaseError
from threading import Event

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .agent_store import HISTORY_LIMIT
from .mail_visuals import icon, sender_display


_DEADLINE_QUESTION = "找出最近一周邮件中的截止日期，帮我整理成待办"


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


class _MailboxScope(QComboBox):
    """Keep both ends of an address legible in the compact window toolbar."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QColor("#f5f7fa"))
        painter.setPen(QColor("#377ac0" if self.hasFocus() else "#d8dfe6"))
        painter.drawRoundedRect(QRectF(bounds), 6, 6)
        middle = bounds.center().y()
        icon("inbox", "#526a82", 15).paint(painter, QRect(8, middle - 7, 15, 15))
        icon("chevron", "#526a82", 12).paint(painter, QRect(self.width() - 18, middle - 6, 12, 12))
        text_rect = bounds.adjusted(28, 0, -22, 0)
        display = self.fontMetrics().elidedText(self.currentText(), Qt.TextElideMode.ElideMiddle, text_rect.width())
        painter.setPen(QColor("#263442"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, display)


class ChatDialog(QDialog):
    open_mail = Signal(object)
    compose_reply = Signal(object, str)
    draft_ready = Signal(object, str)

    tasks_changed = Signal()

    def __init__(self, parent=None, *, accounts, model_description, ask, run, store=None):
        super().__init__(parent)
        self.pages = None
        self.setObjectName("chatDialog")
        self.setWindowTitle("邮箱助手")
        self.resize(700, 660)
        self.setMinimumSize(390, 420)
        self._store = store
        self._run = run
        self._ask = ask
        self._conversation_id = 0
        self._thread_id = None
        self._view_scope = ()
        self._chosen = {}
        self._inputs = {}
        self._live_turns = OrderedDict()
        self._threads = ()
        self._turn_count = 0
        self._offset = 0
        self.conversation_bar = None
        self._pending = None
        self._question_row = None
        self._active_question = ""
        self._destroyed = Event()
        self.destroyed.connect(self._destroyed.set)
        self.history = []
        self.setStyleSheet("""
            QDialog#chatDialog, QWidget#chatTranscript, QWidget#chatWelcome { background: white; color: #252529; }
            QLabel#chatWelcomeTitle { color: #26262b; font-size: 22px; font-weight: bold; }
            QLabel#chatModel, QLabel#chatRole { color: #626b77; font-size: 12px; }
            QLabel#chatQuestion { color: #184471; background: #edf4fd; padding: 9px 13px; border-radius: 11px; }
            QLabel#chatAnswer, QLabel#chatDraftText { color: #26262b; font-size: 14px; }
            QFrame#chatDraftCard { background: #f8fafc; border: 1px solid #dfe5ec; border-radius: 8px; }
            QFrame#deadlineProposal { background: #f6f8fb; border: 1px solid #d9e1eb; border-radius: 9px; }
            QLabel#deadlineHeading { color: #263442; font-size: 15px; font-weight: bold; }
            QLabel#deadlineQuote { color: #364456; background: white; padding: 6px 9px; border-left: 2px solid #8caecc; }
            QPushButton#confirmDeadline { color: white; background: #1469c2; border: 0; border-radius: 6px; padding: 6px 11px; }
            QPushButton#confirmDeadline:hover { background: #0c599e; }
            QPushButton#confirmDeadline:disabled { color: #3e6652; background: #e7f0ea; }
            QScrollArea#chatScroll, QPlainTextEdit#chatInput { border: none; background: white; color: #26262b; }
            QFrame#chatComposer { border: 1px solid #d4d8df; border-radius: 12px; background: white; }
            QPushButton#chatSource, QPushButton#chatSuggestion {
                text-align: left; border: 1px solid #e2e5e9; border-radius: 7px;
                color: #295379; background: #f8fafb; padding: 6px 12px;
            }
            QPushButton#chatSource:hover, QPushButton#chatSuggestion:hover { background: #edf4fc; }
            QPushButton#chatSubmit:enabled { background: #087af2; color: white; border: 0; border-radius: 8px; padding: 6px 14px; }
            QPushButton#chatCompose { background: #edf3fa; color: #1767a9; border: 0; border-radius: 7px; padding: 6px 10px; }
            QPushButton#chatReset { background: transparent; color: #56606e; border: 0; border-radius: 5px; padding: 5px; }
            QPushButton#chatReset:hover, QPushButton#chatReset:focus { background: #eaf0f6; }
            QLabel#chatStatus, QLabel#agentHelp { color: #646470; }
            QTabWidget#agentWorkspace::pane { border: 0; border-top: 1px solid #dce1e7; }
            QTabWidget#agentWorkspace::tab-bar { alignment: left; left: 10px; }
            QTabBar#agentNavigation { background: white; }
            QTabBar#agentNavigation::tab {
                padding: 9px 11px; margin: 5px 1px; background: white;
                border-radius: 7px; color: #495464; font-size: 13px;
            }
            QTabBar#agentNavigation::tab:hover { background: #f1f3f6; }
            QTabBar#agentNavigation::tab:selected { background: #e9f1fb; color: #155d9e; font-weight: bold; }
            QComboBox#agentScope QAbstractItemView { background: white; color: #263442;
                    selection-background-color: #e1eefa; selection-color: #174b7f; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.scope_bar = QWidget()
        top = QHBoxLayout(self.scope_bar)
        top.setContentsMargins(4, 3, 12, 3)
        self.scope = _MailboxScope()
        self.scope.setObjectName("agentScope")
        self.scope.setAccessibleName("查询邮箱范围")
        self.scope.setToolTip("切换此范围的对话和待办")
        self.scope.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scope.setMinimumContentsLength(9)
        self.scope.setFixedHeight(29)
        top.addWidget(self.scope)
        if store is not None:
            from .agent_widgets import PreferencePage, TaskPage
            from .conversation_bar import ConversationBar

            self.pages = QTabWidget()
            self.pages.setObjectName("agentWorkspace")
            self.pages.setDocumentMode(True)
            self.pages.tabBar().setObjectName("agentNavigation")
            self.pages.tabBar().setAccessibleName("助手导航")
            self.pages.tabBar().setDrawBase(False)
            self.pages.tabBar().setUsesScrollButtons(False)
            self.pages.tabBar().setExpanding(False)
            self.pages.setCornerWidget(self.scope_bar)
            top.setContentsMargins(4, 0, 12, 0)
            layout.addWidget(self.pages, 1)
            conversation = QWidget()
            self.pages.addTab(conversation, icon("conversation", "#45647f", 16), "对话")
            self.tasks = TaskPage(store)
            self.tasks.open_mail.connect(self.open_mail)
            self.tasks.changed.connect(self.tasks_changed)
            self.tasks.request_conversation.connect(self._start_deadline_query)
            self.pages.addTab(self.tasks, icon("checklist", "#45647f", 16), "待办")
            self.prefs = PreferencePage(store)
            self.prefs.changed.connect(self.tasks_changed)
            self.pages.addTab(self.prefs, icon("preferences", "#45647f", 16), "偏好")
            self.pages.currentChanged.connect(self._page_changed)
            self._fit_navigation()
            layout = QVBoxLayout(conversation)
            layout.setContentsMargins(0, 0, 0, 12)
            layout.setSpacing(0)
            self.conversation_bar = ConversationBar()
            self.new_chat = self.conversation_bar.new_button
            layout.addWidget(self.conversation_bar)
            self.conversation_bar.selected.connect(self._select_thread)
            self.conversation_bar.new_requested.connect(self._new_thread)
            self.conversation_bar.delete_requested.connect(self._confirm_delete)
            self.page_nav = QWidget()
            nav = QHBoxLayout(self.page_nav)
            nav.setContentsMargins(16, 3, 16, 0)
            self.older_turns = _button("更早记录", "chatReset")
            self.newer_turns = _button("较新记录", "chatReset")
            self.page_label = _label("", name="chatRole")
            self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            nav.addWidget(self.older_turns)
            nav.addWidget(self.page_label, 1)
            nav.addWidget(self.newer_turns)
            self.older_turns.clicked.connect(lambda: self._show_page(self._offset + HISTORY_LIMIT))
            self.newer_turns.clicked.connect(lambda: self._show_page(self._offset - HISTORY_LIMIT))
            layout.addWidget(self.page_nav)
        else:
            self.new_chat = _button("新对话", "chatReset")
            self.new_chat.clicked.connect(self.reset)
            layout.addWidget(self.scope_bar, 0, Qt.AlignmentFlag.AlignRight)
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
            questions.append(_DEADLINE_QUESTION)
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
        if store is None:
            actions.addWidget(self.new_chat)
        self.send_button = _button("提问", "chatSubmit")
        actions.addWidget(self.send_button)
        box.addLayout(actions)
        bottom.addWidget(composer)
        layout.addLayout(bottom)

        self.scope.currentIndexChanged.connect(self.reset)
        self.scope.currentTextChanged.connect(self._scope_tooltip)
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
        inbox_icon = icon("inbox", "#527088", 15)
        self.scope.addItem(inbox_icon, "全部收件箱", "")
        for account in self._accounts:
            self.scope.addItem(inbox_icon, account.address, account.address)
        self.scope.setCurrentIndex(max(0, self.scope.findData(previous)))
        self.scope.blockSignals(False)
        self._scope_tooltip(self.scope.currentText())
        self._ask = ask
        if self.pages is not None:
            self.tasks.set_chat_available(bool(ask and self._accounts))
        self.model_label.setText(f"提问时，问题、偏好和相关邮件节选将交给{model_description}。"
                                 if ask else "启用 AI 后可在这里查信、总结和提取待办。")
        self.reset()

    def _scope_tooltip(self, name):
        self.scope.setToolTip(f"{name} · 切换邮箱范围")

    def _fit_navigation(self):
        if self.pages is not None:
            size = 0 if self.width() < 480 else 16
            self.pages.setIconSize(QSize(size, size))
            self.scope_bar.setFixedHeight(self.pages.tabBar().sizeHint().height())
            self.scope.setFixedWidth(min(220, max(135, self.width() - 285)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_navigation()

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

    def _start_deadline_query(self):
        self.show_conversation()
        if self._pending is None and not self.input.toPlainText().strip():
            self.input.setPlainText(_DEADLINE_QUESTION)
        self.input.setFocus()

    def _page_changed(self, index):
        self.scope_bar.setVisible(self.pages.currentWidget() is not self.prefs)
        if self.pages.currentWidget() is self.tasks:
            self.tasks.refresh()

    def _select_thread(self, thread_id):
        self._chosen[self._view_scope] = thread_id
        self.reset()
        self.input.setFocus()

    def _refresh_conversation_bar(self):
        self._turn_count = next((t.turn_count for t in self._threads if t.id == self._thread_id), 0)
        self.conversation_bar.set_conversations(self._threads, self._thread_id)

    def _new_thread(self):
        if self._thread_id is None:
            self._cancel()
            self.input.clear()
        self._select_thread(None)

    def _confirm_delete(self):
        scope, thread_id = self._view_scope, self._thread_id
        thread = next((item for item in self._threads if item.id == thread_id), None)
        if thread is None:
            return
        prompt = QMessageBox(self)
        prompt.setObjectName("deleteConversationPrompt")
        prompt.setWindowTitle("删除对话")
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setText(f"删除「{thread.title}」的全部聊天记录？")
        prompt.setInformativeText("这段对话删除后无法恢复。")
        cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        delete = prompt.addButton("删除对话", QMessageBox.ButtonRole.DestructiveRole)
        prompt.setDefaultButton(cancel)
        prompt.setEscapeButton(cancel)

        def finished():
            confirmed = prompt.clickedButton() == delete
            prompt.deleteLater()
            if not confirmed:
                return
            try:
                self._store.delete_conversation(scope, thread_id)
            except (DatabaseError, OSError) as exc:
                self._set_status(f"删除对话失败：{exc}")
                return
            key = (scope, thread_id)
            self._inputs.pop(key, None)
            self._live_turns.pop(key, None)
            if self._chosen.get(scope) == thread_id:
                self._chosen.pop(scope, None)
            if self._view_scope == scope:
                self.reset(remember=self._thread_id != thread_id)

        prompt.finished.connect(finished)
        prompt.open()

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

    def _clear_transcript(self):
        while self.messages.count():
            widget = self.messages.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()

    def _render_turns(self, turns):
        self._clear_transcript()
        for turn in turns:
            self._add_question(turn.question)
            self._add_answer(turn)
        self.stack.setCurrentIndex(int(bool(turns)))

    def _show_page(self, offset):
        if self._store is None or self._thread_id is None or self._pending is not None:
            return
        try:
            turns = (self._store.conversation_page(self._view_scope, self._thread_id, offset=offset)
                     if offset else self.history)
        except (DatabaseError, OSError, ValueError) as exc:
            self._set_status(f"读取对话记录失败：{exc}")
            return
        self._offset = offset
        self._render_turns(turns)
        self._update_controls()
        if offset:
            QTimer.singleShot(0, self, lambda: self.scroll.verticalScrollBar().setValue(0))

    def _remember_editor(self):
        key = (self._view_scope, self._thread_id)
        pending_text = self.input.toPlainText()
        if pending_text.strip():
            self._inputs[key] = pending_text
        else:
            self._inputs.pop(key, None)
        # Keep recent interactive cards available when switching back during this run.
        if self._thread_id is not None and any(t.deadline_proposals or t.draft_item for t in self.history):
            self._live_turns[key] = tuple(self.history)
            self._live_turns.move_to_end(key)
            if len(self._live_turns) > 4:
                self._live_turns.popitem(last=False)

    def reset(self, *, restore=True, remember=True):
        self._conversation_id += 1
        self._cancel()
        if self._store is not None and self._view_scope and remember and restore:
            self._remember_editor()
        self.history.clear()
        self._clear_transcript()
        self.input.clear()
        self.stack.setCurrentIndex(0)
        self._set_status("请先绑定邮箱。" if not self._accounts else "")
        if self._store is not None:
            scope = tuple(sorted(self._scope_addresses()))
            self._view_scope = scope
            self._thread_id = None
            self._threads = ()
            self._offset = 0
            self.tasks.scope = scope
            if self.pages.currentWidget() is self.tasks:
                self.tasks.refresh()
            if scope and restore:
                try:
                    self._threads = self._store.list_conversations(scope)
                    newest = self._threads[0].id if self._threads else None
                    selected = self._chosen.get(scope, newest)
                    if selected is not None and all(t.id != selected for t in self._threads):
                        selected = newest
                    self._thread_id = self._chosen[scope] = selected
                    key = (scope, selected)
                    if selected is not None:
                        turns = self._live_turns.pop(key, None)
                        if turns is None:
                            turns = self._store.conversation_page(scope, selected)
                        self.history.extend(turns)
                    self.input.setPlainText(self._inputs.pop(key, ""))
                except (DatabaseError, OSError) as exc:
                    self._set_status(f"读取对话记忆失败：{exc}")
                self._render_turns(self.history)
            self._refresh_conversation_bar()
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
        if self.conversation_bar is None:
            self.new_chat.setVisible(bool(self.history or busy or self.input.toPlainText()))
        else:
            self.new_chat.setEnabled(bool(self._accounts))
            count = self._turn_count
            self.page_nav.setVisible(count > HISTORY_LIMIT)
            self.older_turns.setEnabled(not busy and self._offset + HISTORY_LIMIT < count)
            self.newer_turns.setEnabled(not busy and self._offset > 0)
            if count > HISTORY_LIMIT:
                end = count - self._offset
                self.page_label.setText(f"第 {max(1, end - HISTORY_LIMIT + 1)}–{end} 轮，共 {count} 轮")
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
        if self._offset:
            self._show_page(0)
        address = self.scope.currentData()
        accounts = tuple(a for a in self._accounts if not address or a.address == address)
        scope, thread_id = self._view_scope, self._thread_id
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
            if cancelled() or not isValid(self) or self._pending is not stop:
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
                try:
                    if thread_id is None:
                        self._thread_id = self._store.start_conversation(scope, turn)
                        self._chosen[scope] = self._thread_id
                    else:
                        self._store.save_turn(scope, turn, conversation_id=thread_id)
                except (ValueError, DatabaseError, OSError) as exc:
                    self._set_status(f"本轮对话仅保留在窗口中，保存失败：{exc}")
                else:
                    try:
                        self._threads = self._store.list_conversations(scope)
                        self._refresh_conversation_bar()
                    except (DatabaseError, OSError) as exc:
                        self._set_status(f"对话已保存，刷新会话列表失败：{exc}")
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
                    card.confirmed.connect(lambda p=proposal: self._deadline_added(p))
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

    def _deadline_added(self, proposal):
        # A confirmed card must not become editable again after switching threads.
        for index, turn in enumerate(self.history):
            if any(p is proposal for p in turn.deadline_proposals):
                remaining = tuple(p for p in turn.deadline_proposals if p is not proposal)
                self.history[index] = replace(turn, deadline_proposals=remaining)
        self.tasks_changed.emit()

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
