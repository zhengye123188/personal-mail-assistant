"""A reply composer bound to one message in one mailbox."""

from dataclasses import replace
from threading import Event

from PySide6.QtCore import QEvent, QPointF, Qt, Signal, QTimer
from PySide6.QtGui import QPainter, QPalette, QTextCursor, QTextLayout
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from shiboken6 import isValid


_INSTRUCTION_LIMIT = 2000


def _label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _button(text, name):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class _InstructionsEdit(QPlainTextEdit):
    submitted = Signal()
    completionChanged = Signal(str)
    completionContextChanged = Signal()

    def __init__(self, suggestions):
        super().__init__()
        self._composing = False
        self._suggestions = tuple(s for s in suggestions if s and len(s) <= _INSTRUCTION_LIMIT)
        self._model_candidate = ""
        self._candidate_prefix = ""
        self._last_prefix = None
        self.completion_suffix = ""
        self.textChanged.connect(self._refresh_completion)
        self.cursorPositionChanged.connect(self._refresh_completion)
        self.selectionChanged.connect(self._refresh_completion)
        self._refresh_completion()

    def _refresh_completion(self):
        typed = self.toPlainText()
        cursor = self.textCursor()
        suffix = ""
        eligible = None
        if (self.isEnabled() and not self.isReadOnly() and not self._composing
                and cursor.atEnd() and not cursor.hasSelection()):
            eligible = typed
            prefix, candidates = typed, self._suggestions
            if self._model_candidate and typed.startswith(self._candidate_prefix) and self._model_candidate.startswith(typed):
                candidates = (self._model_candidate, *candidates)
            if typed.startswith(("请帮我", "帮我")):
                prefix = self._without_request_intro(typed)
                candidates = tuple(self._without_request_intro(s) for s in candidates)
            if prefix not in candidates:
                suffix = next((s[len(prefix):] for s in candidates if s.startswith(prefix)
                               and len(typed) + len(s) - len(prefix) <= _INSTRUCTION_LIMIT), "")
        if not typed:
            placeholder = suffix or ("输入你的回复要求…" if not self._composing else "")
            self.setPlaceholderText(placeholder)
        if suffix != self.completion_suffix:
            self.completion_suffix = suffix
            self.setAccessibleDescription(f"建议补全：{suffix}。按 Tab 采用。" if suffix else "")
            self.completionChanged.emit(suffix)
            self.viewport().update()
        if eligible != self._last_prefix:
            self._last_prefix = eligible
            self.completionContextChanged.emit()

    def set_model_candidate(self, candidate, prefix=""):
        self._model_candidate = candidate
        self._candidate_prefix = prefix
        self._refresh_completion()

    def model_prefix(self):
        prefix = self._last_prefix
        if prefix is not None and len(prefix) <= 320 and not prefix.endswith(("。", "！", "？", "?", "!", "\n")):
            if not prefix or prefix not in (*self._suggestions, self._model_candidate):
                return prefix
        return None

    @staticmethod
    def _without_request_intro(text):
        return text.removeprefix("请帮我").removeprefix("帮我").lstrip(" \t")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.ReadOnlyChange):
            self._refresh_completion()

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)
        self._refresh_completion()

    def event(self, event):
        # Qt dispatches focus-traversal keys before keyPressEvent.
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Tab
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            if self._composing:
                event.accept()
                return True
            if self.completion_suffix:
                cursor = self.textCursor()
                cursor.beginEditBlock()
                cursor.insertText(self.completion_suffix)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                event.accept()
                return True
        return super().event(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        # Qt paints the empty-field placeholder. For a prefix, lay out the suffix
        # at the caret on the viewport only, leaving the editable document intact.
        if not self.completion_suffix or self.document().isEmpty():
            return
        caret = self.cursorRect()
        margin = self.document().documentMargin()
        left = self.contentOffset().x() + margin
        right = self.viewport().width() - margin
        x, y = float(caret.right()), float(caret.top())
        if right - x < self.fontMetrics().horizontalAdvance(self.completion_suffix[0]):
            x, y = left, y + caret.height()
        layout = QTextLayout(self.completion_suffix.replace("\n", "\u2028"), self.font(), self.viewport())
        layout.setTextOption(self.document().defaultTextOption())
        layout.beginLayout()
        while y < self.viewport().height():
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1.0, right - x))
            line.setPosition(QPointF(x, y))
            y += line.height()
            x = left
        layout.endLayout()
        painter = QPainter(self.viewport())
        painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        layout.draw(painter, QPointF())
        painter.end()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if self._composing:
                event.accept()
                return
            if event.modifiers() in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.MetaModifier):
                self.submitted.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class ReplyDialog(QDialog):
    sent = Signal()
    configure_requested = Signal()
    model_completion_toggled = Signal(bool)

    def __init__(self, parent=None, *, account, recipient, subject, model_description="",
                 generate=None, converse=None, draft_with=None, suggestions=(), complete=None, send, run):
        super().__init__(parent)
        self._send = send if recipient else None
        self._run = run
        self._state = "idle"
        self._pending_generation = None
        self._destroyed = Event()
        self.destroyed.connect(self._destroyed.set)
        self._model_status = False
        self.assistant_history = []
        self._converse = None
        self._draft_with = None
        self._generate = None
        self._complete = None
        self._completion_job = None
        self._last_request = None
        self._completion_timer = QTimer(self)
        self._completion_timer.setSingleShot(True)
        self._completion_timer.setInterval(700)
        self._completion_timer.timeout.connect(self._fetch_completion)
        self.setObjectName("replyDialog")
        self.setMinimumSize(410, 425)
        self.setStyleSheet("""
            QDialog#replyDialog, QScrollArea#composeScroll, QWidget#composePage { background: white; }
            QLineEdit[headerValue="true"] { border: none; background: transparent;
                color: #242427; padding: 1px 0; }
            QLabel#modelHint, QLabel#replyStatus, QLabel#completionHint { color: #626269; font-size: 12px; }
            QLabel#draftLabel, QLabel#instructionsLabel { color: #30333a; font-weight: bold; }
            QLabel#instructionLimit { color: #a43e32; font-size: 12px; }
            QPlainTextEdit#replyInstructions { color: #222226; background: #fafbfd;
                border: 1px solid #bfc9d5; border-radius: 8px; padding: 6px 8px; }
            QPlainTextEdit#replyInstructions:focus { background: white; border-color: #5295e3; }
            QPushButton#generateReply:enabled { color: #15548f; background: #e8f1fc;
                border: 1px solid #ccdef1; border-radius: 6px; padding: 4px 12px; }
            QPushButton#generateReply:enabled:hover { background: #daeafd; }
            QPlainTextEdit#replyDraft { color: #222226; background: white;
                border: 1px solid #d3d3d8; border-radius: 7px; padding: 9px; }
            QPlainTextEdit#replyDraft:focus { border-color: #5295e3; }
            QPlainTextEdit#replyConversation { background: #f4f7fb; color: #202b3b;
                border: 1px solid #dfe6ef; border-radius: 7px; padding: 7px; font-size: 14px; }
            QPushButton#sendReply:enabled { background: #087af2; color: white; }
            QPushButton#sendReply:enabled:hover { background: #066bd6; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        scroll = QScrollArea()
        scroll.setObjectName("composeScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("composePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 13, 20, 4)
        layout.setSpacing(6)
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)
        headers = QFormLayout()
        headers.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        headers.setVerticalSpacing(4)
        headers.setHorizontalSpacing(13)
        for name, value in (("发件人", account), ("收件人", recipient or "未找到可回复地址"), ("主题", subject)):
            field = QLineEdit(value)
            field.setReadOnly(True)
            field.setProperty("headerValue", True)
            field.setAccessibleName(name)
            field.setCursorPosition(0)
            field.setToolTip(value)
            headers.addRow(name, field)
        layout.addLayout(headers)

        self.ai_tools = QWidget()
        self.ai_tools.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        ai_layout = QVBoxLayout(self.ai_tools)
        ai_layout.setContentsMargins(0, 4, 0, 0)
        ai_layout.setSpacing(5)
        instructions_label = _label("告诉 AI 怎么回")
        instructions_label.setObjectName("instructionsLabel")
        self.configure_button = _button("模型设置…", "configureReplyModel")
        self.configure_button.setFlat(True)
        self.configure_button.clicked.connect(self.configure_requested)
        ai_header = QHBoxLayout()
        ai_header.addWidget(instructions_label)
        self.completion_hint = _label("按 Tab 采纳")
        self.completion_hint.setObjectName("completionHint")
        ai_header.addWidget(self.completion_hint)
        ai_header.addStretch()
        self.model_completion = QCheckBox("模型补全")
        self.model_completion.setToolTip("开启后，当前模型将根据来信主题、正文节选和你输入的开头给出 Tab 建议。")
        self.model_completion.toggled.connect(self._completion_mode_changed)
        self.model_completion.clicked.connect(lambda: self.model_completion_toggled.emit(self.model_completion.isChecked()))
        ai_header.addWidget(self.model_completion)
        ai_header.addWidget(self.configure_button)
        ai_layout.addLayout(ai_header)
        self.instructions = _InstructionsEdit(suggestions)
        self.instructions.setObjectName("replyInstructions")
        self.instructions.setAccessibleName("告诉 AI 怎么回")
        self.completion_hint.setVisible(bool(self.instructions.completion_suffix))
        self.instructions.completionChanged.connect(lambda suffix: self.completion_hint.setVisible(bool(suffix)))
        self.instructions.completionContextChanged.connect(self._schedule_completion)
        self.instructions.setTabChangesFocus(True)
        self.instructions.setFixedHeight(82)
        instructions_label.setBuddy(self.instructions)
        ai_layout.addWidget(self.instructions)
        self.generate_button = _button("按要求起草", "generateReply")
        self.generate_button.setToolTip("按输入的要求撰写回复（⌘ / Ctrl + 回车）")
        self.ask_button = _button("询问来信", "askAboutMail")
        self.ask_button.setToolTip("把上面的文字作为问题，查看助手对来信的解答")
        self.instruction_limit = _label("")
        self.instruction_limit.setObjectName("instructionLimit")
        ai_actions = QHBoxLayout()
        ai_actions.addWidget(self.generate_button)
        ai_actions.addWidget(self.ask_button)
        ai_actions.addStretch()
        ai_actions.addWidget(self.instruction_limit)
        ai_layout.addLayout(ai_actions)
        self.model_hint = _label("")
        self.model_hint.setObjectName("modelHint")
        ai_layout.addWidget(self.model_hint)
        self.assistant_transcript = QPlainTextEdit()
        self.assistant_transcript.setObjectName("replyConversation")
        self.assistant_transcript.setAccessibleName("本封邮件的助手对话")
        self.assistant_transcript.setReadOnly(True)
        self.assistant_transcript.setTabChangesFocus(True)
        self.assistant_transcript.setMinimumHeight(49)
        self.assistant_transcript.setMaximumHeight(82)
        self.assistant_transcript.hide()
        ai_layout.addWidget(self.assistant_transcript)
        layout.addWidget(self.ai_tools)

        self.draft = QPlainTextEdit()
        self.draft.setObjectName("replyDraft")
        self.draft.setAccessibleName("回复正文")
        self.draft.setPlaceholderText("在此撰写回复…")
        self.draft.setTabChangesFocus(True)
        self.draft.setMinimumHeight(86)
        self.draft_label = _label("回复正文")
        self.draft_label.setObjectName("draftLabel")
        self.draft_label.setBuddy(self.draft)
        layout.addWidget(self.draft_label)
        layout.addWidget(self.draft, 1)
        self.status = _label("")
        self.status.setObjectName("replyStatus")
        self.status.hide()
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.setContentsMargins(20, 4, 20, 12)
        self.copy_button = _button("复制正文", "copyReply")
        self.send_button = _button("发送回复", "sendReply")
        actions.addWidget(self.copy_button)
        actions.addStretch()
        actions.addWidget(self.send_button)
        outer.addLayout(actions)

        self.generate_button.clicked.connect(lambda: self._start_generation(asking=False))
        self.ask_button.clicked.connect(lambda: self._start_generation(asking=True))
        self.instructions.submitted.connect(self.generate_button.click)
        self.copy_button.clicked.connect(self._copy)
        self.send_button.clicked.connect(self._start_send)
        self.instructions.textChanged.connect(self._update_controls)
        self.draft.textChanged.connect(self._update_controls)
        self.set_model(model_description, generate, converse, draft_with, complete=complete)
        if self.ai_tools.isHidden():
            self.resize(620, 550)
            self.draft.setFocus()
        else:
            self.resize(640, 650)
            self.instructions.setFocus()

    @property
    def sending(self):
        return self._state == "sending"

    @property
    def has_pending_draft(self):
        return (not self.draft.document().isEmpty() or self._pending_generation is not None
                or bool(self.instructions.toPlainText().strip()))

    @property
    def has_session_content(self):
        return self.has_pending_draft or bool(self.assistant_history)

    def restore_conversation(self, turns):
        """Continue a mailbox conversation already scoped by the caller to this message."""
        if self.assistant_history or (self._converse is None and self._draft_with is None):
            return
        self.assistant_history.extend(turns)
        if self.assistant_history:
            recent = self.assistant_history[-3:]
            self.assistant_transcript.appendPlainText(f"来自邮箱助手 · 最近 {len(recent)} 轮")
            for turn in recent:
                self._append_exchange(self._brief(turn.question, 90), self._brief(turn.answer, 180))

    @staticmethod
    def _brief(text, limit):
        text = " ".join(text.split())
        return text[:limit].rstrip() + "…" if len(text) > limit else text

    def set_model(self, description, generate, converse=None, draft_with=None, *, complete=None):
        self._clear_completion()
        self._complete = complete
        self.model_completion.setVisible(complete is not None)
        if self._pending_generation is not None:
            self._pending_generation.set()
            self._pending_generation = None
            self._state = "idle"
        if self._model_status:
            self._show_status("")
        self.assistant_history.clear()
        self.assistant_transcript.clear()
        self.assistant_transcript.hide()
        self._generate = generate
        self._converse = converse
        self._draft_with = draft_with
        can_write = generate is not None or draft_with is not None
        enabled = can_write or converse is not None
        self.ask_button.setVisible(converse is not None)
        self.generate_button.setVisible(can_write)
        self.ai_tools.setVisible(enabled)
        self.draft_label.setVisible(enabled)
        title = "回复"
        hint = ""
        if enabled:
            title = "回复与助手"
            hint = f"{description} 将根据来信节选、你的要求和草稿作答。"
        self.setWindowTitle(title)
        self.model_hint.setText(hint)
        self._update_controls()
        self._schedule_completion()

    def _clear_completion(self):
        self._completion_timer.stop()
        if self._completion_job is not None:
            self._completion_job.set()
            self._completion_job = None
        self._last_request = None
        self.instructions.set_model_candidate("")

    def _completion_mode_changed(self):
        self._clear_completion()
        self._schedule_completion()

    def _completion_prefix(self):
        if (self._complete and self.model_completion.isChecked() and self._state == "idle"
                and self.isActiveWindow() and self.isVisible()):
            return self.instructions.model_prefix()
        return None

    def _schedule_completion(self):
        self._completion_timer.stop()
        prefix = self._completion_prefix()
        if prefix is not None and prefix != self._last_request:
            self._completion_timer.start()

    def _fetch_completion(self):
        prefix = self._completion_prefix()
        if prefix is None or self._completion_job is not None or prefix == self._last_request:
            return
        complete = self._complete
        cancelled = Event()
        self._completion_job = cancelled
        self._last_request = prefix

        def work():
            return "" if cancelled.is_set() else complete(prefix)

        def finished(suffix, error):
            if cancelled.is_set() or not isValid(self):
                return
            self._completion_job = None
            if self._completion_prefix() == prefix:
                if not error and isinstance(suffix, str) and suffix:
                    self.instructions.set_model_candidate(prefix + suffix, prefix)
            else:
                self._schedule_completion()

        self._run(work, finished)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_completion()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and hasattr(self, "instructions"):
            if self.isActiveWindow():
                self._schedule_completion()
            elif self._completion_job is not None:
                self._clear_completion()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._clear_completion()

    def _update_controls(self):
        editable = self._state not in ("sending", "sent")
        idle = self._state == "idle"
        has_draft = not self.draft.document().isEmpty()
        can_send = has_draft and bool(self.draft.toPlainText().strip())
        can_write = self._draft_with is not None or self._generate is not None
        ai_ready = idle and (can_write or self._converse is not None)
        self.instructions.setReadOnly(not ai_ready)
        self.configure_button.setEnabled(ai_ready)
        self.draft.setReadOnly(not editable)
        self.copy_button.setEnabled(can_send)
        length = len(self.instructions.toPlainText().strip())
        over_limit = length > _INSTRUCTION_LIMIT
        self.instruction_limit.setVisible(over_limit)
        if over_limit:
            self.instruction_limit.setText(f"请删减 {length - _INSTRUCTION_LIMIT} 字（最多 {_INSTRUCTION_LIMIT} 字）")
        ready = ai_ready and 0 < length <= _INSTRUCTION_LIMIT
        self.generate_button.setEnabled(ready and can_write)
        self.ask_button.setEnabled(ready and self._converse is not None)
        self.generate_button.setText("按要求修改" if can_send else "按要求起草")
        self.send_button.setEnabled(idle and can_send and self._send is not None)
        self.send_button.setText("发送中…" if self.sending else "发送回复")

    def _show_status(self, message, *, model=False):
        self._model_status = model
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def _start_generation(self, *, asking):
        instruction = self.instructions.toPlainText().strip()
        if self._state != "idle" or not 0 < len(instruction) <= _INSTRUCTION_LIMIT:
            return
        operation = self._converse if asking else self._draft_with
        generate = None if asking else self._generate
        if operation is None and generate is None:
            return
        draft = self.draft.toPlainText()
        history = tuple(self.assistant_history)
        cancelled = Event()
        lifetime = self._destroyed
        self._pending_generation = cancelled
        revision = self.draft.document().revision()
        self._state = "generating"
        self._update_controls()
        self._show_status("助手正在阅读来信…" if asking else "正在按你的要求写回复…", model=True)

        def work():
            if not cancelled.is_set() and not lifetime.is_set():
                if operation is not None:
                    return operation(instruction, draft, history)
                return generate(instruction)

        def finished(result, error):
            if cancelled.is_set() or not isValid(self):
                return
            self._pending_generation = None
            self._state = "idle"
            if error:
                self._show_status(f"助手请求失败。{error}", model=True)
            elif operation is not None:
                if asking and result is not None and result.draft:
                    result = replace(result, draft=None)
                self._show_turn(result, revision)
            elif self.draft.document().revision() != revision:
                self._show_status("已保留你在生成期间编辑的草稿。", model=True)
            elif isinstance(result, str) and result.strip():
                self._replace_draft(result.strip())
                self.instructions.clear()
                self._show_status("")
            else:
                self._show_status("模型返回了空草稿，请重新生成。", model=True)
            self._update_controls()

        self._run(work, finished)

    def _replace_draft(self, text):
        cursor = self.draft.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        self.draft.setTextCursor(cursor)

    def _show_turn(self, turn, revision):
        if turn is None or (not turn.answer and not turn.draft):
            self._show_status("助手没有返回内容，请重试。", model=True)
            return
        self.assistant_history.append(turn)
        suggestion = ""
        status = ""
        if turn.draft:
            if self.draft.document().revision() == revision:
                self._replace_draft(turn.draft)
                status = "已更新回复正文，可继续编辑或撤销。"
            else:
                suggestion = turn.draft
                status = "已保留你刚编辑的正文，助手的建议草稿可在对话中复制。"
        self._append_exchange(turn.question, turn.answer, suggestion)
        self.instructions.clear()
        self._show_status(status, model=True)

    def _append_exchange(self, question, answer, suggestion=""):
        exchange = [f"你　{question}", f"助手　{answer}" if answer else "助手已拟好回复。"]
        if suggestion:
            exchange.extend(["建议草稿", suggestion])
        transcript = self.assistant_transcript
        prefix = "" if transcript.document().isEmpty() else "\n"
        transcript.appendPlainText(prefix + "\n".join(exchange))
        transcript.show()
        transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _copy(self):
        QApplication.clipboard().setText(self.draft.toPlainText())
        self._show_status("回复正文已复制。")

    def _start_send(self):
        text = self.draft.toPlainText()
        if self._state != "idle" or not text.strip() or self._send is None:
            return
        self._state = "sending"
        self._update_controls()
        self._show_status("正在发送回复…")

        def finished(_, error):
            if not isValid(self):
                return
            if error:
                self._state = "idle"
                self._show_status(str(error))
                self._update_controls()
                return
            self._state = "sent"
            self._update_controls()
            self.sent.emit()
            self.accept()

        self._run(lambda: self._send(text), finished)

    def reject(self):
        if not self.sending:
            super().reject()

    def closeEvent(self, event):
        if self.sending:
            event.ignore()
        else:
            super().closeEvent(event)
