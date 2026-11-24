"""Chinese desktop interface for the personal mail assistant."""

import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from itertools import islice
from threading import Event
from time import monotonic

from PySide6.QtCore import QObject, QRect, QSettings, QSize, Qt, QThreadPool, QTimer, QRunnable, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .chat_dialog import ChatDialog
from .compose_assistant import ComposeTurn, compose_draft, compose_turn
from .models import MailItem, ReplyDraft, ReplyEnvelope
from .mail_visuals import MailDelegate, icon
from .providers import MAIL_PROVIDERS, provider_for_address
from .reader import ReaderPane
from .remote_images import download_images
from .reminder_popup import ReminderPopup
from .reply_dialog import ReplyDialog
from .reply_settings import ModelSettingsDialog, saved_choice, selected_model
from .reply_suggestions import model_completer, reply_suggestions
from .service import CredentialAccessPaused, KeyringVault, MailboxAuthorizationRequired


USER_ROLE = Qt.ItemDataRole.UserRole
NAV_KIND = int(USER_ROLE) + 1
NAV_COUNT = int(USER_ROLE) + 2
IMAGE_BATCH_SIZE = 3
INBOX_REFRESH_INTERVAL = 60
_FILTERS = {"all": "全部邮件", "unread": "仅未读", "flagged": "仅有旗标"}


@dataclass
class _MailboxFlags:
    unread_keys: set
    flagged_count: int


@dataclass
class _ReplyDraft:
    dialog: ReplyDialog
    item: MailItem
    envelope: ReplyEnvelope | None
    original_text: str
    save_timer: QTimer

    @property
    def subject(self):
        return self.item.subject


class _SidebarDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        kind = index.data(NAV_KIND)
        return QSize(145, 36 if kind == "section" else 48 if kind == "account" else 33)

    def paint(self, painter, option, index):
        kind = index.data(NAV_KIND)
        rect = option.rect.adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        font = QFont(option.font)
        if kind == "section":
            font.setPixelSize(12)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor("#73737a"))
            painter.drawText(rect.adjusted(8, 0, 0, -5), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, index.data())
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected or option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e4e5e9" if selected else "#eeeeef"))
            painter.drawRoundedRect(rect, 7, 7)
        glyph = {"unread": "unread", "flagged": "flag", "pending": "checklist", "drafts": "draft"}.get(kind, "inbox")
        icon(glyph, "#0879e6" if selected else "#787a81").paint(
            painter, QRect(rect.left() + 8, rect.center().y() - 9, 19, 19))
        color = "#0969ca" if selected else "#27282c"
        left = rect.left() + 35
        count = index.data(NAV_COUNT)
        right = rect.right() - 7
        if count:
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor("#717178"))
            width = QFontMetrics(font).horizontalAdvance(str(count)) + 6
            painter.drawText(QRect(right - width, rect.top(), width, rect.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(count))
            right -= width + 2
        font.setPixelSize(14)
        painter.setFont(font)
        painter.setPen(QColor(color))
        width = max(1, right - left)
        title = QFontMetrics(font).elidedText(index.data(), Qt.TextElideMode.ElideRight, width)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if kind == "account":
            painter.drawText(QRect(left, rect.top() + 4, width, 21), align, title)
            font.setPixelSize(11)
            painter.setFont(font)
            painter.setPen(QColor("#676870"))
            address = QFontMetrics(font).elidedText(index.data(USER_ROLE), Qt.TextElideMode.ElideMiddle, width)
            painter.drawText(QRect(left, rect.top() + 25, width, 16), align, address)
        else:
            painter.drawText(QRect(left, rect.top(), width, rect.height()), align, title)
        painter.restore()


_STYLE = """
QMainWindow, QWidget#mailSurface, QWidget#listPane, QWidget#readingColumn, QDialog {
    background: white; color: #202124;
}
QLabel#viewHeading, QDialog QLabel { color: #202124; }
QFrame#sidebar { background: #f5f5f7; border-radius: 16px; }
QPushButton#mailChat, QPushButton#deadlineTasks {
    text-align: left; color: #244667; border: 0; border-radius: 7px; padding: 7px 12px; }
QPushButton#mailChat { background: #e2ecf7; }
QPushButton#mailChat:hover { background: #d7e6f7; }
QPushButton#deadlineTasks { background: transparent; }
QPushButton#deadlineTasks:hover { background: #e7eaf0; }
QListWidget { border: 0; outline: none; background: transparent; }
QSplitter::handle { background: #e4e4e7; }
QFrame#toolbarGroup { background: #f3f3f5; border-radius: 16px; }
QPushButton[tool="true"] { border: 0; background: transparent; border-radius: 13px; padding: 0; }
QPushButton[tool="true"]:hover { background: #e4e5e8; }
QPushButton[tool="true"]:pressed { background: #d6d7dc; }
QPushButton[tool="true"]:checked { background: #deebfa; }
QLineEdit#mailSearch { color: #202124; background: #f3f3f5; border: 1px solid transparent;
    border-radius: 16px; padding: 4px 8px; font-size: 14px; selection-background-color: #b6d8ff; }
QLineEdit#mailSearch:focus { border: 1px solid #6da9ed; }
QLabel#viewHeading { font-size: 16px; font-weight: 650; }
QLabel#listStatus, QLabel#emptyListLabel { color: #727278; font-size: 12px; }
QLabel#listError, QLabel#bindStatus { color: #99511c; }
QPushButton#bindButton, QPushButton#updateBindingButton, QPushButton#loadOlderButton, QPushButton#retryListButton,
QPushButton#aiSettings {
    background: transparent; color: #096ec9; border: 0; padding: 5px 7px; border-radius: 6px; }
QPushButton#bindButton:hover, QPushButton#updateBindingButton:hover, QPushButton#loadOlderButton:hover,
QPushButton#retryListButton:hover, QPushButton#aiSettings:hover { background: #e7eaf0; }
QLabel#dialogHeading { font-size: 19px; font-weight: bold; } QLabel#muted { color: #68686f; }
QDialog QLineEdit, QDialog QComboBox { color: #242427; background: #fff;
    border: 1px solid #ceced2; border-radius: 6px; padding: 6px; }
QDialog QPushButton { color: #222; background: #f0f0f2; border: none; border-radius: 6px; padding: 6px 14px; }
QDialog QPushButton#submitBindingButton { background: #087af2; color: white; }
QDialog QPushButton:disabled { color: #89898d; background: #e5e5e8; }
"""


def _tool_button(name, glyph, tip, handler):
    button = _button("", name)
    button.setProperty("tool", True)
    button.setIcon(icon(glyph))
    button.setIconSize(QSize(18, 18))
    button.setFixedSize(31, 29)
    button.setToolTip(tip)
    button.setAccessibleName(tip)
    button.clicked.connect(handler)
    return button


def _group(*buttons):
    group = QFrame()
    group.setObjectName("toolbarGroup")
    line = QHBoxLayout(group)
    line.setContentsMargins(3, 2, 3, 2)
    line.setSpacing(1)
    for button in buttons:
        line.addWidget(button)
    return group


class _Signals(QObject):
    finished = Signal(int, object, object)


class _Job(QRunnable):
    def __init__(self, number, function, signals, stop=None):
        super().__init__()
        self.number = number
        self.function = function
        self.signals = signals
        self.stop = stop

    def run(self):
        try:
            result = None if self.stop is not None and self.stop.is_set() else self.function()
        except Exception as exc:
            self.signals.finished.emit(self.number, None, exc)
        else:
            self.signals.finished.emit(self.number, result, None)


def _label(text="", name="", wrap=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _button(text, name):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _key(item):
    return item.account, item.validity, item.uid


class _BindDialog(QDialog):
    def __init__(self, parent, account):
        super().__init__(parent)
        self.setObjectName("bindDialog")
        self.is_update = account is not None
        self.setWindowTitle(f"更新{MAIL_PROVIDERS[account.provider].credential_label}" if account else "绑定邮箱")
        self.setMinimumWidth(420)
        self.busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        layout.addWidget(_label(self.windowTitle(), "dialogHeading"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(11)
        self.address = QLineEdit()
        self.address.setObjectName("addressEdit")
        self.address.setAccessibleName("邮箱账号，@ 前的部分")
        self.address.setPlaceholderText("输入 @ 前的部分")
        self.domain = QComboBox()
        self.domain.setObjectName("domainCombo")
        self.domain.setAccessibleName("邮箱后缀")
        for code, profile in MAIL_PROVIDERS.items():
            for domain in profile.domains:
                self.domain.addItem("@" + domain, code)
        address_row = QHBoxLayout()
        address_row.setSpacing(6)
        address_row.addWidget(self.address, 1)
        address_row.addWidget(self.domain)
        self.secret = QLineEdit()
        self.secret.setObjectName("secretEdit")
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.credential_label = QLabel()
        self.credential_label.setBuddy(self.secret)
        if account:
            local, _, domain = account.address.partition("@")
            self.domain.setCurrentText("@" + domain)
            self.domain.setEnabled(False)
            self.address.setText(local)
            self.address.setReadOnly(True)
        address_label = QLabel("邮箱地址")
        address_label.setBuddy(self.address)
        form.addRow(address_label, address_row)
        form.addRow(self.credential_label, self.secret)
        layout.addLayout(form)
        self.setup_hint = _label("", "muted", True)
        layout.addWidget(self.setup_hint)
        self.help_link = QLabel()
        self.help_link.setOpenExternalLinks(True)
        layout.addWidget(self.help_link)
        self.status = _label("", "bindStatus", True)
        self.status.hide()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.submit = buttons.addButton("保存" if account else "绑定邮箱", QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit.setObjectName("submitBindingButton")
        self.submit.setDefault(True)
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._domain_changed()
        self.domain.currentIndexChanged.connect(self._domain_changed)
        self.address.editingFinished.connect(self._split_full_address)
        (self.secret if account else self.address).setFocus()

    def _split_full_address(self):
        address = self.address.text().strip()
        if provider_for_address(address):
            local, _, domain = address.partition("@")
            index = self.domain.findText("@" + domain.lower())
            if index >= 0:
                self.domain.setCurrentIndex(index)
                self.address.setText(local)

    def email_address(self):
        self._split_full_address()
        local = self.address.text().strip()
        if not local or "@" in local:
            return ""
        return local + self.domain.currentText()

    def _domain_changed(self):
        profile = MAIL_PROVIDERS[self.domain.currentData()]
        self.credential_label.setText(profile.credential_label)
        self.secret.clear()
        self.secret.setPlaceholderText(f"请输入{profile.credential_label}")
        self.setup_hint.setText(profile.setup_hint)
        self.help_link.setText(f'<a href="{profile.help_url}">查看{profile.name}的官方设置说明</a>')
        self.show_status("")

    def set_busy(self, busy):
        self.busy = busy
        self.cancel.setText("关闭" if busy else "取消")
        self.submit.setEnabled(not busy)
        self.address.setEnabled(not busy)
        self.secret.setEnabled(not busy)
        self.domain.setEnabled(not busy and not self.is_update)

    def show_status(self, message):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def reject(self):
        if self.busy:
            self.secret.clear()
            self.hide()
        else:
            super().reject()


class MainWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.setWindowTitle("个人邮件助手")
        self.accounts = []
        self.items = {}
        self.cursors = {}
        self.failed = {}
        self.loading_accounts = set()
        self.seen_during_page_load = {}
        self.head_requested_at = {}
        self.refresh_after_load = set()
        self.polling = set()
        self.reminder_errors = {}
        self.authorization_needed = set()
        self.authorizing = None
        self.queue_error = ""
        self.queue_items = {}
        self.notified_key = None
        self.finishing = set()
        self.closing = False
        self.stop_sync = Event()
        self.active_address = None
        self.body_id = 0
        self.image_download_stop = Event()
        self.reading_item = None
        self.body_ready = False
        self.marking_read = set()
        self.job_id = 0
        self.jobs = {}
        self.binding = None
        self.reply_prefs = QSettings("PersonalMailAssistant", "ReplyModels")
        self.reply_choice = saved_choice(self.reply_prefs)
        self.model_completions = self.reply_prefs.value("modelCompletions", False, type=bool)
        self.reply_model = selected_model(self.reply_choice, self.reply_prefs)
        self.model_settings = None
        self.chat_dialog = None
        self.reply_envelope = None
        self.reply_drafts = {}
        self.saved_drafts = {}
        self.dirty_drafts = set()
        self.sent_cleanup = {}
        self.draft_errors = {}
        self.draft_list_error = ""
        # Each active worker retains the emitter even if the window is destroyed.
        self.signals = _Signals()
        self.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.pool = QThreadPool.globalInstance()
        self.filter_mode = "all"
        self.nav_kind = "all"
        self.mailbox_flags = {}
        self._build_window()
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.hide()
        bar.messageChanged.connect(lambda message: bar.setVisible(bool(message)))
        self.popup = ReminderPopup(self)
        self.popup.openRequested.connect(self._open_pending)
        self.deadline_popup = None
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(60_000)
        self.reminder_timer.timeout.connect(self._poll_reminders)
        self.reminder_timer.timeout.connect(self._poll_deadlines)
        self._load_accounts()
        self.reminder_timer.start()

    def _build_window(self):
        self.resize(1280, 755)
        self.setMinimumSize(760, 470)
        self.setStyleSheet(_STYLE)
        self.setWindowIcon(icon("envelope", "#087df3", 40))
        self.native_title = (sys.platform == "darwin" and QApplication.platformName() == "cocoa"
                             and hasattr(Qt.WindowType, "ExpandedClientAreaHint"))
        if self.native_title:
            self.setWindowFlag(Qt.WindowType.ExpandedClientAreaHint, True)
            self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint, True)
            self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        central = QWidget()
        central.setObjectName("mailSurface")
        if self.native_title:
            central.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 7, 0, 7)
        layout.setSpacing(0)
        self.sidebar = self._build_sidebar()
        layout.addWidget(self.sidebar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_list())
        self.splitter.addWidget(self._build_reader())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([345, 710])
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(central)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)
        QShortcut(QKeySequence.StandardKey.Refresh, self, activated=self._manual_refresh)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, activated=self._toggle_sidebar)

    def showEvent(self, event):
        super().showEvent(event)
        if self.native_title:
            from .mac_window import hide_title_text
            hide_title_text(self)

    def closeEvent(self, event):
        for draft in self.reply_drafts.values():
            if draft.dialog.sending:
                draft.dialog.show()
                draft.dialog.raise_()
                event.ignore()
                return
        if not self._flush_drafts():
            event.ignore()
            self.statusBar().showMessage("草稿未能保存，请在草稿箱查看详情后重试。")
            return
        self.closing = True
        self.stop_sync.set()
        if self.binding is not None:
            self.binding.close()
        if self.chat_dialog is not None:
            self.chat_dialog.disable()
        if self.model_settings is not None:
            self.model_settings.close()
        for draft in list(self.reply_drafts.values()):
            draft.dialog.set_model("", None)
            draft.dialog.close()
            draft.dialog.deleteLater()
        self.reply_drafts.clear()
        self.image_download_stop.set()
        self.reminder_timer.stop()
        self.popup.hide()
        if self.deadline_popup is not None:
            self.deadline_popup.hide()
        super().closeEvent(event)

    def _build_sidebar(self):
        pane = QFrame()
        pane.setObjectName("sidebar")
        pane.setFixedWidth(192)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(8, 0, 8, 11)
        layout.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(6, 4, 4, 8)
        top.addStretch()
        top.addWidget(_tool_button("hideSidebar", "sidebar", "隐藏侧边栏", self._toggle_sidebar))
        layout.addLayout(top)
        self.account_list = QListWidget()
        self.account_list.setObjectName("accountList")
        self.account_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.account_list.setMouseTracking(True)
        self.account_list.setItemDelegate(_SidebarDelegate(self.account_list))
        self.account_list.currentItemChanged.connect(self._account_changed)
        layout.addWidget(self.account_list, 1)
        self.chat_button = _button("邮箱助手", "mailChat")
        self.chat_button.setIcon(icon("conversation", "#36628b", 16))
        self.chat_button.setToolTip("跨邮箱查信、总结来信、提取截止日期或起草回复")
        self.chat_button.clicked.connect(self._open_chat)
        self.chat_button.setVisible(self.reply_model is not None)
        layout.addWidget(self.chat_button)
        self.deadlines_button = _button("截止待办", "deadlineTasks")
        self.deadlines_button.setIcon(icon("checklist", "#466786", 16))
        self.deadlines_button.setToolTip("查看已确认的截止时间、原邮件和完成状态")
        self.deadlines_button.clicked.connect(self._open_tasks)
        layout.addWidget(self.deadlines_button)
        self.ai_settings_button = _button("AI 设置…", "aiSettings")
        self.ai_settings_button.setToolTip("设置邮箱助手、辅助回复及使用的模型")
        self.ai_settings_button.clicked.connect(self._open_model_settings)
        layout.addWidget(self.ai_settings_button)
        self.update_button = _button("更新邮箱凭据", "updateBindingButton")
        self.update_button.clicked.connect(self._update_account)
        layout.addWidget(self.update_button)
        bind = _button("添加邮箱账户", "bindButton")
        bind.setIcon(icon("plus", "#0877da", 15))
        bind.clicked.connect(lambda: self._open_binding(None))
        layout.addWidget(bind)
        return pane

    def _build_list(self):
        pane = QWidget()
        pane.setObjectName("listPane")
        pane.setMinimumWidth(225)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(14, 3, 10, 9)
        header.setSpacing(6)
        self.show_sidebar = _tool_button("showSidebar", "sidebar", "显示侧边栏", self._toggle_sidebar)
        self.show_sidebar.hide()
        header.addWidget(self.show_sidebar)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.heading = _label("全部收件箱", "viewHeading")
        self.status = _label("", "listStatus")
        for label in (self.heading, self.status):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            titles.addWidget(label)
        header.addLayout(titles, 1)
        self.filter_button = _tool_button("filterButton", "filter", "筛选当前收件箱", self._show_filters)
        self.refresh_button = _tool_button("refreshButton", "refresh", "刷新收件箱", self._manual_refresh)
        header.addWidget(_group(self.filter_button, self.refresh_button))
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setLayout(header)
        layout.addWidget(bar)
        self.notice = QWidget()
        notice_row = QHBoxLayout(self.notice)
        notice_row.setContentsMargins(14, 0, 8, 5)
        self.notice_label = _label("", "listError", True)
        self.retry_button = _button("重试", "retryListButton")
        self.retry_button.clicked.connect(self._retry_list)
        notice_row.addWidget(self.notice_label, 1)
        notice_row.addWidget(self.retry_button)
        layout.addWidget(self.notice)
        self.list_stack = QStackedWidget()
        self.mail_list = QListWidget()
        self.mail_list.setObjectName("messageList")
        self.mail_list.setMouseTracking(True)
        self.mail_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mail_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.mail_delegate = MailDelegate(self.mail_list)
        self.mail_list.setItemDelegate(self.mail_delegate)
        self.mail_list.currentItemChanged.connect(self._mail_changed)
        self.mail_list.itemActivated.connect(self._activate_row)
        self.list_stack.addWidget(self.mail_list)
        self.empty_list = _label("", "emptyListLabel", True)
        self.empty_list.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_list.setMargin(12)
        self.list_stack.addWidget(self.empty_list)
        layout.addWidget(self.list_stack, 1)
        self.older_button = _button("载入更早的邮件", "loadOlderButton")
        self.older_button.clicked.connect(self._load_older)
        layout.addWidget(self.older_button)
        return pane

    def _build_reader(self):
        pane = QWidget()
        pane.setObjectName("readingColumn")
        pane.setMinimumWidth(270)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.previous_button = _tool_button("previousMessage", "previous", "上一封邮件",
                                            lambda: self._step_message(-1))
        self.next_button = _tool_button("nextMessage", "next", "下一封邮件",
                                        lambda: self._step_message(1))
        self.copy_button = _tool_button("copyMessage", "copy", "复制邮件正文",
                                        lambda: QApplication.clipboard().setText(self.body.toPlainText().replace("\ufffc", "")))
        self.reply_button = _tool_button("replyMessage", "reply", "回复这封邮件", self._open_reply)
        self.reply_button.setText("回复")
        self.reply_button.setFixedWidth(64)
        self.reply_button.setEnabled(False)
        self.discard_button = _tool_button("discardDraft", "trash", "删除草稿", self._confirm_discard)
        self.discard_button.hide()
        self.search = QLineEdit()
        self.search.setObjectName("mailSearch")
        self.search.setPlaceholderText("搜索")
        self.search.setAccessibleName("搜索已载入的邮件")
        self.search.setToolTip("在已载入邮件的发件人、主题、摘要和邮箱地址中搜索")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMinimumWidth(92)
        self.search.setMaximumWidth(310)
        self.search.setFixedHeight(33)
        self.search.textChanged.connect(self._apply_local_view)
        toolbar = QWidget()
        toolbar.setFixedHeight(52)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(10, 0, 12, 7)
        row.setSpacing(8)
        row.addWidget(_group(self.previous_button, self.next_button))
        row.addWidget(_group(self.reply_button, self.discard_button, self.copy_button))
        row.addStretch(1)
        row.addWidget(self.search, 4)
        layout.addWidget(toolbar)
        self.reader = ReaderPane()
        self.subject, self.meta, self.body = self.reader.subject, self.reader.meta, self.reader.body
        self.body_retry = self.reader.retry
        self.body_retry.clicked.connect(self._retry_body)
        self.reader.seen_retry.clicked.connect(self._retry_seen)
        self.reader.complete.clicked.connect(self._complete_selected)
        self.reader.load_images.clicked.connect(lambda: self._load_external_images(retry=True))
        layout.addWidget(self.reader, 1)
        return pane

    def _toggle_sidebar(self):
        hide = not self.sidebar.isHidden()
        self.sidebar.setVisible(not hide)
        self.show_sidebar.setVisible(hide)
        # On macOS the traffic lights continue to occupy the top left when the rail is hidden.
        self.show_sidebar.parentWidget().layout().setContentsMargins(
            84 if hide and self.native_title else 14, 3, 10, 9)

    def _step_message(self, delta):
        target = self.mail_list.currentRow() + delta
        if 0 <= target < self.mail_list.count():
            self.mail_list.setCurrentRow(target)

    def _show_filters(self):
        menu = QMenu(self)
        group = QActionGroup(menu)
        for mode, title in _FILTERS.items():
            action = QAction(title, group)
            action.setCheckable(True)
            action.setChecked(self.filter_mode == mode)
            action.triggered.connect(lambda checked=False, value=mode: self._set_filter(value))
            menu.addAction(action)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.filter_button.mapToGlobal(self.filter_button.rect().bottomLeft()))

    def _set_filter(self, mode):
        if self.nav_kind in ("unread", "flagged") and mode != self.nav_kind:
            for row in range(self.account_list.count()):
                if self.account_list.item(row).data(NAV_KIND) == mode:
                    self.account_list.setCurrentRow(row)
                    return
        self.filter_mode = mode
        self._apply_local_view()

    def _apply_local_view(self):
        if self.reading_item is not None and self.mail_list.currentItem() is None:
            self._mail_changed(None)
        self._render_rows()
        self._list_state()

    def _run(self, operation, callback, *, stop=None):
        self.job_id += 1
        self.jobs[self.job_id] = callback
        self.pool.start(_Job(self.job_id, operation, self.signals, stop))

    @Slot(int, object, object)
    def _finished(self, number, result, error):
        callback = self.jobs.pop(number, None)
        if callback and not self.closing:
            callback(result, error)

    def _read_queue(self):
        try:
            self.queue_items = {_key(m): m for m in self.service.list_pending()}
            self.queue_error = ""
        except Exception as exc:
            self.queue_error = str(exc)

    def _read_drafts(self):
        try:
            records = {_key(d.item): d for d in self.service.list_drafts(include_original=False)}
        except Exception as exc:
            self.draft_list_error = f"无法读取本机草稿：{exc}"
            return
        # Keep edits that are still waiting for a successful local write.
        for key in self.dirty_drafts:
            if key in self.saved_drafts:
                records[key] = self.saved_drafts[key]
            else:
                records.pop(key, None)
        for key in self.sent_cleanup:
            records.pop(key, None)
        self.saved_drafts = records
        self.draft_list_error = ""

    @staticmethod
    def _draft_row(draft):
        envelope = draft.envelope
        recipient = envelope.recipient if envelope else ""
        stamp = datetime.fromtimestamp(draft.updated_at).strftime("%Y-%m-%d %H:%M") if draft.updated_at else ""
        return replace(draft.item, subject=envelope.subject if envelope else draft.item.subject,
                       sender=recipient or "未提供回复地址", recipient=recipient, unread=False, flagged=False,
                       timestamp=draft.updated_at, date=stamp, preview=draft.text or draft.instructions)

    def _show_draft(self, draft):
        row = self._draft_row(draft)
        self.reader.show_item(replace(row, sender=row.account))
        self.reader.mailbox.setText("本机草稿箱")
        self.reader.set_body(draft.text or f"回复要求\n{draft.instructions}")
        self.copy_button.setEnabled(bool(draft.text))
        self.body_ready = True
        self.reply_button.setEnabled(True)

    def _draft_changed(self, key):
        if self.nav_kind == "drafts":
            self._render_rows()
            selected = self.mail_list.currentItem()
            if selected is not None and _key(selected.data(USER_ROLE)) == key:
                self._show_draft(self.saved_drafts[key])
        self._list_state()

    def _save_edited_draft(self, key):
        if key not in self.dirty_drafts:
            return
        session = self.reply_drafts.get(key)
        if session:
            session.save_timer.stop()
            draft = ReplyDraft(session.item, session.envelope, session.original_text,
                               session.dialog.draft.toPlainText(), session.dialog.instructions.toPlainText())
        else:
            draft = self.saved_drafts[key]
        try:
            draft = self.service.save_draft(draft)
        except Exception as exc:
            self.draft_errors[key] = f"保存《{draft.item.subject}》失败：{exc}"
            self.statusBar().showMessage(self.draft_errors[key])
        else:
            self.draft_errors.pop(key, None)
            self.dirty_drafts.discard(key)
        if draft.text.strip() or draft.instructions.strip():
            self.saved_drafts[key] = draft
        else:
            self.saved_drafts.pop(key, None)
        self._draft_changed(key)

    def _delete_draft_record(self, item):
        key = _key(item)
        try:
            self.service.delete_draft(item)
        except Exception as exc:
            self.draft_errors[key] = f"清理《{item.subject}》的本机草稿失败：{exc}"
            self.statusBar().showMessage(self.draft_errors[key])
            return False
        self.draft_errors.pop(key, None)
        self.sent_cleanup.pop(key, None)
        self.saved_drafts.pop(key, None)
        return True

    def _flush_drafts(self):
        for key in tuple(self.dirty_drafts):
            self._save_edited_draft(key)
        for item in tuple(self.sent_cleanup.values()):
            self._delete_draft_record(item)
        return not (self.dirty_drafts or self.sent_cleanup)

    def _confirm_discard(self):
        if self.nav_kind != "drafts" or self.reading_item is None:
            return
        key = _key(self.reading_item)
        draft = self.saved_drafts.get(key)
        session = self.reply_drafts.get(key)
        if draft is None or (session and session.dialog.sending):
            return
        prompt = QMessageBox(self)
        prompt.setObjectName("discardDraftPrompt")
        prompt.setWindowTitle("删除草稿")
        prompt.setText(f"删除《{draft.item.subject}》的本机草稿？")
        cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        discard = prompt.addButton("删除草稿", QMessageBox.ButtonRole.DestructiveRole)
        prompt.setDefaultButton(cancel)
        prompt.setEscapeButton(cancel)

        def finished():
            session = self.reply_drafts.get(key)
            if (prompt.clickedButton() == discard and not (session and session.dialog.sending)
                    and self._delete_draft_record(draft.item)):
                self.dirty_drafts.discard(key)
                self.reply_drafts.pop(key, None)
                if session:
                    session.save_timer.stop()
                    session.dialog.set_model("", None)
                    session.dialog.close()
                    session.dialog.deleteLater()
                self._draft_changed(key)
            prompt.deleteLater()

        prompt.finished.connect(finished)
        prompt.open()

    def _poll_reminders(self, accounts=None):
        for account in (self.accounts if accounts is None else accounts):
            address = account.address
            if address in self.polling:
                continue
            self.polling.add(address)

            def done(arrivals, error, address=address):
                self.polling.discard(address)
                self._track_authorization(address, error)
                if error:
                    self.reminder_errors[address] = str(error)
                else:
                    self.reminder_errors.pop(address, None)
                    self._read_queue()
                    if arrivals:
                        self.head_requested_at.pop(address, None)
                        self.notified_key = _key(max(arrivals, key=lambda mail: (mail.timestamp, mail.uid)))
                        self.popup.notify(len(arrivals))
                    if self.nav_kind == "pending":
                        self._render_rows()
                self._list_state()

            self._run(lambda a=account: self.service.poll_pending(a), done, stop=self.stop_sync)
        self._list_state()

    def _poll_deadlines(self):
        # Ordinary inbox use never creates an agent database or contacts a model.
        if self.closing or not self.accounts or not self.service.agent_store_path.is_file():
            return
        try:
            tasks = self.service.agent_store.claim_due(self.accounts)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.statusBar().showMessage(f"截止提醒检查失败：{exc}", 10_000)
            return
        if tasks:
            if self.deadline_popup is None:
                self.deadline_popup = ReminderPopup(self, deadlines=True)
                self.deadline_popup.openRequested.connect(self._open_tasks)
            self.deadline_popup.notify_deadlines(tasks)

    def _open_pending(self):
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.clear()
        self.account_list.setCurrentItem(self.pending_mailbox)
        for row in range(self.mail_list.count()):
            if _key(self.mail_list.item(row).data(USER_ROLE)) == self.notified_key:
                self.mail_list.setCurrentRow(row)
                return
        if self.mail_list.count():
            self.mail_list.setCurrentRow(0)

    def _complete_selected(self):
        row = self.mail_list.currentItem()
        if row is None:
            return
        item = row.data(USER_ROLE)
        key = _key(item)
        if key not in self.queue_items or key in self.finishing:
            return
        self.finishing.add(key)
        self.reader.complete.setEnabled(False)

        def done(_, error):
            self.finishing.discard(key)
            if error:
                self.queue_error = f"无法标为已处理：{error}"
            else:
                self._read_queue()
                if not self.queue_items:
                    self.popup.hide()
            if self.nav_kind == "pending":
                self._render_rows()
            self._list_state()

        self._run(lambda: self.service.complete_pending(item), done)

    def _manual_refresh(self):
        if self.nav_kind != "drafts":
            KeyringVault.resume_access()
        self._refresh()
        if self.nav_kind != "drafts":
            self._poll_reminders()

    def _load_accounts(self, select=None):
        known = {a.address for a in self.accounts}
        try:
            self.accounts = list(self.service.accounts())
        except Exception as exc:
            self.account_list.clear()
            self.accounts = []
            self._update_chat()
            self._clear_list()
            self._list_state()
            self.notice_label.setText(f"无法载入账号：{exc}")
            self.notice.show()
            self.update_button.hide()
            self.retry_button.show()
            return
        self._update_chat()
        self.account_list.blockSignals(True)
        self.account_list.clear()
        def section(title):
            entry = QListWidgetItem(title, self.account_list)
            entry.setData(NAV_KIND, "section")
            entry.setFlags(Qt.ItemFlag.NoItemFlags)

        def mailbox(title, kind, address=None):
            entry = QListWidgetItem(title, self.account_list)
            entry.setData(NAV_KIND, kind)
            if address:
                entry.setData(USER_ROLE, address)
                entry.setToolTip(address)
            return entry

        section("个人收藏")
        selected = mailbox("全部收件箱", "all")
        self.pending_mailbox = mailbox("待处理", "pending")
        self.pending_mailbox.setToolTip("查看需要回复、确认或跟进的新邮件")
        mailbox("草稿箱", "drafts").setToolTip("打开保存在本机的回复草稿")
        mailbox("未读邮件", "unread")
        mailbox("有旗标", "flagged")
        for account in self.accounts:
            section(MAIL_PROVIDERS[account.provider].name)
            entry = mailbox("收件箱", "account", account.address)
            if select == account.address:
                selected = entry
        self.account_list.setCurrentItem(selected)
        self.account_list.blockSignals(False)
        for account in self.accounts:
            if account.address not in known:
                page = self.service.cached_first_page(account)
                if page is not None:
                    self.items.update((_key(m), m) for m in page.items)
        if select:
            self.head_requested_at.pop(select, None)
            if select in self.loading_accounts:
                self.refresh_after_load.add(select)
        self._read_queue()
        self._read_drafts()
        self._account_changed(self.account_list.currentItem())
        self._poll_reminders()
        self._poll_deadlines()

    def _open_binding(self, account):
        if self.binding is not None:
            self.binding.show()
            self.binding.raise_()
            self.binding.activateWindow()
            return
        dialog = _BindDialog(self, account)
        self.binding = dialog
        dialog.finished.connect(lambda: self._release_binding(dialog))
        dialog.submit.clicked.connect(lambda: self._bind(dialog))
        dialog.open()

    def _release_binding(self, dialog):
        dialog.secret.clear()
        if self.binding is dialog:
            self.binding = None
        dialog.deleteLater()

    def _bind(self, dialog):
        if dialog.busy:
            return
        address = dialog.email_address()
        provider = dialog.domain.currentData()
        secret = dialog.secret.text()
        if not address:
            dialog.show_status("请填写 @ 前的邮箱账号，并从列表中选择对应后缀。")
            dialog.address.setFocus()
            return
        if not secret.strip():
            dialog.show_status(f"请填写{MAIL_PROVIDERS[provider].credential_label}。")
            dialog.secret.setFocus()
            return
        KeyringVault.resume_access()
        dialog.set_busy(True)
        dialog.show_status("正在验证邮箱并保存绑定，可关闭此窗口。")

        def done(account, error):
            if self.binding is not dialog:
                return
            dialog.set_busy(False)
            if error:
                if dialog.isVisible():
                    dialog.show_status(str(error))
                    dialog.secret.setFocus()
                else:
                    self.statusBar().showMessage(f"绑定 {address} 失败：{error}")
                    dialog.reject()
                return
            select = account.address if dialog.isVisible() else self.active_address
            dialog.accept()
            self._load_accounts(select)

        self._run(lambda: self.service.bind(provider, address, secret), done)

    def _update_account(self):
        account = next((a for a in self.accounts if a.address == self.active_address), None)
        if account:
            self._open_binding(account)

    def _account_changed(self, item, previous=None):
        if item is None or item.data(NAV_KIND) == "section":
            return
        kind = item.data(NAV_KIND)
        address = item.data(USER_ROLE)
        inbox_views = ("all", "unread", "flagged")
        reuse = (previous is not None
                 and self.nav_kind in inbox_views and kind in inbox_views)
        self.active_address = address
        self.nav_kind = kind
        self.filter_mode = kind if kind in ("unread", "flagged") else "all"
        self.mail_delegate.show_address = self.active_address is None
        account = next((a for a in self.accounts if a.address == self.active_address), None)
        title = f"收件箱 — {MAIL_PROVIDERS[account.provider].name}" if account else item.text()
        self.heading.setText(title)
        self.heading.setToolTip(account.address if account else title)
        self.update_button.setVisible(account is not None)
        if account:
            self.update_button.setText(f"更新{MAIL_PROVIDERS[account.provider].credential_label}")
        self.filter_button.setVisible(kind not in ("pending", "drafts"))
        editing = kind == "drafts"
        self.reply_button.setText("继续编辑" if editing else "回复")
        self.reply_button.setFixedWidth(82 if editing else 64)
        self.reply_button.setAccessibleName("继续编辑草稿" if editing else "回复这封邮件")
        self.reply_button.setToolTip(self.reply_button.accessibleName())
        self.discard_button.setVisible(editing)
        if not reuse:
            self._clear_list()
        self._refresh(force=False)

    def _targets(self):
        return [a for a in self.accounts if self.active_address is None or a.address == self.active_address]

    def _clear_list(self):
        self.mail_list.blockSignals(True)
        self.mail_list.clear()
        self.mail_list.blockSignals(False)
        self.mail_list.verticalScrollBar().setValue(0)
        self._mail_changed(None)

    def _refresh(self, *, force=True):
        if self.nav_kind == "drafts":
            self._flush_drafts()
            self._read_drafts()
            self._apply_local_view()
            return
        if self.nav_kind == "pending":
            self._read_queue()
            self._apply_local_view()
            return
        if force:
            self.mail_list.verticalScrollBar().setValue(0)
        else:
            self._apply_local_view()
        now = monotonic()
        self._request_pages([(a, None) for a in self._targets()
                             if force or a.address not in self.head_requested_at
                             or now - self.head_requested_at[a.address] >= INBOX_REFRESH_INTERVAL])

    def _load_older(self):
        self._request_pages([(a, self.cursors[a.address]) for a in self._targets()
                             if self.cursors.get(a.address) is not None and a.address not in self.failed])

    def _retry_list(self):
        account = self._authorization_target()
        if account:
            self._authorize_sync(account)
            return
        if self.nav_kind in ("pending", "drafts"):
            self._manual_refresh()
            return
        if not self.accounts:
            self._load_accounts()
            return
        KeyringVault.resume_access()
        requests = [(a, self.failed[a.address][0]) for a in self._targets() if a.address in self.failed]
        self._request_pages(requests)

    def _track_authorization(self, address, error):
        if isinstance(error, (MailboxAuthorizationRequired, CredentialAccessPaused)):
            self.authorization_needed.add(address)
        elif error is None:
            self.authorization_needed.discard(address)

    def _authorization_target(self):
        if self.nav_kind != "drafts":
            return next((account for account in self._targets()
                         if account.address in self.authorization_needed), None)
        return None

    def _authorize_sync(self, account):
        if self.authorizing:
            return
        address = self.authorizing = account.address
        KeyringVault.resume_access()
        self._list_state()

        def done(_, error):
            self.authorizing = None
            if error:
                self.failed[address] = (None, str(error))
                self.reminder_errors[address] = str(error)
                if not isinstance(error, (MailboxAuthorizationRequired, CredentialAccessPaused)):
                    self.authorization_needed.discard(address)
                self._list_state()
                return
            self.authorization_needed.discard(address)
            self.reminder_errors.pop(address, None)
            if address in self.loading_accounts:
                self.refresh_after_load.add(address)
            else:
                self._request_pages([(account, None)])
            self._poll_reminders([account])
            if self.reading_item and self.reading_item.account == address and self.nav_kind != "drafts":
                if not self.body_ready:
                    self._mail_changed(self.mail_list.currentItem())
                else:
                    self._sync_seen()
            self._list_state()

        self._run(lambda: self.service.authorize_mailbox(account), done, stop=self.stop_sync)

    def _request_pages(self, requests):
        for account, cursor in requests:
            if account.address in self.loading_accounts:
                continue
            self.loading_accounts.add(account.address)
            if cursor is None:
                self.head_requested_at[account.address] = monotonic()
            self.seen_during_page_load[account.address] = set()
            self.failed.pop(account.address, None)

            def done(page, error, account=account, cursor=cursor):
                self.loading_accounts.discard(account.address)
                confirmed = self.seen_during_page_load.pop(account.address)
                self._track_authorization(account.address, error)
                if error:
                    self.failed[account.address] = (cursor, str(error))
                    self.mailbox_flags.pop(account.address, None)
                else:
                    self.cursors[account.address] = page.next_cursor
                    incoming = {_key(m): replace(m, unread=False) if _key(m) in confirmed else m
                                for m in page.items}
                    removed = ({k for k in self.items if k[0] == account.address} - incoming.keys()
                               if cursor is None else set())
                    # A chat citation can open an older message while its first page is in flight.
                    opened = _key(self.reading_item) if self.reading_item else None
                    if opened in removed and page.items and page.items[0].validity == opened[1]:
                        if page.date_cursor:
                            if opened[2] in page.date_cursor.ordered_uids:
                                removed.discard(opened)
                        elif page.before_uid is not None and opened[2] < page.before_uid:
                            removed.discard(opened)
                    changed = bool(removed) or any(self.items.get(k) != m for k, m in incoming.items())
                    for key in removed:
                        del self.items[key]
                    self.items.update(incoming)
                    if page.next_cursor is None:
                        scoped = [m for m in self.items.values() if m.account == account.address]
                        if all(m.unread is not None and m.flagged is not None for m in scoped):
                            self.mailbox_flags[account.address] = _MailboxFlags(
                                {_key(m) for m in scoped if m.unread}, sum(bool(m.flagged) for m in scoped))
                        else:
                            self.mailbox_flags.pop(account.address, None)
                    else:
                        self.mailbox_flags.pop(account.address, None)
                    if (changed and self.nav_kind not in ("pending", "drafts")
                            and self.active_address in (None, account.address)):
                        self._render_rows()
                if account.address in self.refresh_after_load:
                    self.refresh_after_load.remove(account.address)
                    self._request_pages([(account, None)])
                self._list_state()

            self._run(lambda a=account, c=cursor: self.service.list_messages(a, c), done, stop=self.stop_sync)
        self._list_state()

    def _render_rows(self, keep_reading=None):
        old = self.mail_list.currentItem()
        selected = _key(old.data(USER_ROLE)) if old is not None else None
        scroll = self.mail_list.verticalScrollBar().value()
        self.mail_list.blockSignals(True)
        self.mail_list.clear()
        query = self.search.text().strip().casefold()
        if self.nav_kind == "drafts":
            items = (self._draft_row(draft) for draft in self.saved_drafts.values())
        else:
            items = (self.queue_items if self.nav_kind == "pending" else self.items).values()
            if self.active_address:
                items = (m for m in items if m.account == self.active_address)
        for item in sorted(items, key=lambda m: (m.timestamp or 0, m.uid, m.account), reverse=True):
            if ((self.filter_mode == "unread" and not item.unread)
                    or (self.filter_mode == "flagged" and not item.flagged)):
                continue
            if query and not any(query in value.casefold() for value in (item.sender, item.subject,
                                                                          item.preview, item.account)):
                continue
            row = QListWidgetItem(f"{item.sender}\n{item.subject}\n{item.account}")
            row.setToolTip(f"{item.account}\n{item.subject}")
            row.setData(USER_ROLE, item)
            self.mail_list.addItem(row)
            if selected == _key(item):
                self.mail_list.setCurrentItem(row)
        self.mail_list.blockSignals(False)
        self.mail_list.verticalScrollBar().setValue(scroll)
        source = self.queue_items if self.nav_kind == "pending" else self.items
        removed = (self.nav_kind != "drafts" and self.reading_item is not None
                   and _key(self.reading_item) not in source)
        if removed or (selected is not None and selected != keep_reading and self.mail_list.currentItem() is None):
            self._mail_changed(None)

    def _list_state(self):
        in_queue = self.nav_kind == "pending"
        in_drafts = self.nav_kind == "drafts"
        addresses = {a.address for a in self._targets()}
        loading = bool(addresses & self.loading_accounts)
        failures = addresses.intersection(self.failed)
        has_older = any(self.cursors.get(a) is not None and a not in failures for a in addresses)
        if in_drafts:
            error_text = "\n".join(filter(None, [self.draft_list_error, *self.draft_errors.values()]))
        elif in_queue:
            issues = [f"{a} 提醒检查失败：{message}" for a, message in self.reminder_errors.items()]
            if self.queue_error:
                issues.append(self.queue_error)
            error_text = "\n".join(issues)
        else:
            error_text = "\n".join(f"{a}：{self.failed[a][1]}" for a in sorted(failures))
        account = self._authorization_target()
        if account:
            error_text = (f"正在等待 {self.authorizing} 的钥匙串授权。" if self.authorizing
                          else f"{account.address} 需要授权。桌面版会将该邮箱的授权码保存在系统钥匙串中，供下次自动同步。")
        self.retry_button.setText("授权并记住" if account else "重试")
        self.retry_button.setToolTip(f"授权并记住 {account.address}" if account else "")
        self.retry_button.setEnabled(not (account and self.authorizing))
        self.notice_label.setText(error_text)
        self.notice.setVisible(bool(error_text))
        visible = self.mail_list.count()
        filtered = bool(self.search.text().strip()) or self.filter_mode != "all"
        if in_drafts:
            self.status.setText(f"{visible} 封{'匹配的' if filtered else ''}本机草稿")
        elif in_queue:
            summary = f"{visible} 封{'匹配' if filtered else '待处理'}"
            if self.polling:
                summary += " · 正在检查新邮件…"
            self.status.setText(summary)
        elif loading:
            self.status.setText(f"已显示 {visible} 封 · 正在同步…" if visible else "正在收取邮件…")
        elif failures and visible:
            self.status.setText(f"已显示 {visible} 封 · 同步未完成，可重试")
        elif filtered:
            self.status.setText(f"已载入邮件中有 {visible} 封匹配")
        else:
            scoped = [m for m in self.items.values() if m.account in addresses]
            count = len(scoped)
            partial = bool(failures) or has_older
            summary = f"{'已载入 ' if partial else ''}{count} 封邮件"
            if count and all(m.unread is not None for m in scoped):
                summary += f"，{sum(bool(m.unread) for m in scoped)} 封未读"
            self.status.setText(summary)
        self.status.setToolTip(self.status.text())
        self.filter_button.setIcon(icon("filter", "#007aff" if self.filter_mode != "all" else "#646469"))
        self.filter_button.setToolTip(f"筛选已载入邮件 · {_FILTERS[self.filter_mode]}")
        self._update_navigation()
        known_flags = [self.mailbox_flags[a.address] for a in self.accounts if a.address in self.mailbox_flags]
        complete = bool(self.accounts) and len(known_flags) == len(self.accounts)
        for i in range(self.account_list.count()):
            row = self.account_list.item(i)
            kind = row.data(NAV_KIND)
            count = None
            if kind == "account":
                flags = self.mailbox_flags.get(row.data(USER_ROLE))
                count = len(flags.unread_keys) if flags else None
            elif kind == "drafts":
                count = len(self.saved_drafts)
            elif kind == "pending":
                count = len(self.queue_items)
                if self.reminder_errors or self.queue_error:
                    count = f"{count} !" if count else "!"
                    row.setToolTip("部分提醒未能更新，点开查看详情")
                else:
                    row.setToolTip("查看需要回复、确认或跟进的新邮件")
            elif complete and kind in _FILTERS:
                count = sum(flags.flagged_count if kind == "flagged" else len(flags.unread_keys)
                            for flags in known_flags)
            if row.data(NAV_COUNT) != count:
                row.setData(NAV_COUNT, count)
        self.refresh_button.setEnabled(bool(self.accounts) and (in_queue or in_drafts or not loading)
                                       and not (in_queue and self.polling))
        self.older_button.setVisible(has_older and not (in_queue or in_drafts))
        self.older_button.setEnabled(not loading)
        if visible:
            self.list_stack.setCurrentWidget(self.mail_list)
        else:
            self.list_stack.setCurrentWidget(self.empty_list)
            if in_drafts:
                message = "没有匹配的草稿" if filtered else "暂无草稿"
            elif not self.accounts:
                message = "绑定邮箱后即可查看收件箱"
            elif in_queue:
                if filtered:
                    message = "没有匹配的待处理邮件"
                elif self.queue_error:
                    message = "待处理列表未能加载"
                else:
                    message = "暂无待处理邮件\n新的来信需要回复或确认时，会显示在这里。"
            elif loading:
                message = "正在打开收件箱…"
            elif filtered and any(m.account in addresses for m in self.items.values()):
                message = "已载入邮件中没有匹配项"
            elif failures:
                message = "邮件未能加载"
            else:
                message = "收件箱暂无邮件"
            self.empty_list.setText(message)

    def _retry_body(self):
        KeyringVault.resume_access()
        self._mail_changed(self.mail_list.currentItem())

    def _retry_seen(self):
        KeyringVault.resume_access()
        self._sync_seen()

    def _update_chat(self):
        model = self.reply_model
        self.chat_button.setVisible(model is not None)
        self.chat_button.setEnabled(bool(self.accounts))
        self.deadlines_button.setVisible(bool(self.accounts))
        if self.chat_dialog is None:
            return
        if model is None:
            self.chat_dialog.disable()
            self.chat_dialog.deleteLater()
            self.chat_dialog = None
        else:
            self.chat_dialog.reconfigure(accounts=tuple(self.accounts), model_description=model.description,
                                        ask=self._agent_ask())

    def _agent_ask(self):
        if self.reply_model is not None:
            from .mail_agent import MailAgent
            return MailAgent(self.service, self.reply_model, self.service.agent_store).ask
        return None

    def _present_chat(self, *, tasks=False):
        if not self.accounts or (not tasks and self.reply_model is None):
            return
        if self.chat_dialog is None:
            try:
                store = self.service.agent_store
                self.chat_dialog = ChatDialog(
                    self, accounts=tuple(self.accounts),
                    model_description=self.reply_model.description if self.reply_model else "",
                    ask=self._agent_ask(), run=self._run, store=store,
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.statusBar().showMessage(f"无法打开邮箱助手：{exc}")
                return
            self.chat_dialog.open_mail.connect(self._open_chat_source)
            self.chat_dialog.compose_reply.connect(self._open_chat_draft)
            self.chat_dialog.draft_ready.connect(self._save_chat_draft)
            self.chat_dialog.tasks_changed.connect(self._poll_deadlines)
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        if tasks:
            self.chat_dialog.show_tasks(account="")
        else:
            self.chat_dialog.show_conversation()
        self.chat_dialog.show()
        self.chat_dialog.raise_()
        self.chat_dialog.activateWindow()

    def _open_chat(self):
        self._present_chat()

    def _open_tasks(self):
        self._present_chat(tasks=True)

    def _open_chat_source(self, item):
        if self.chat_dialog is None:
            return
        for index in range(self.account_list.count()):
            entry = self.account_list.item(index)
            if entry.data(NAV_KIND) == "account" and entry.data(USER_ROLE) == item.account:
                break
        else:
            self.chat_dialog.show_error("这封邮件所属的账号已不可用。")
            return
        self.account_list.setCurrentItem(entry)
        self.filter_mode = "all"
        self.search.clear()
        key = _key(item)
        # The cited message can be older than the pages already in the inbox.
        self.items.setdefault(key, item)
        self._render_rows()
        self._list_state()
        for index in range(self.mail_list.count()):
            row = self.mail_list.item(index)
            if _key(row.data(USER_ROLE)) == key:
                self.mail_list.setCurrentItem(row)
                self.mail_list.scrollToItem(row)
                break
        self.chat_dialog.close()
        self.raise_()
        self.activateWindow()

    def _save_chat_draft(self, draft, prior_text):
        key = _key(draft.item)
        session = self.reply_drafts.get(key)
        saved = self.saved_drafts.get(key)
        if session:
            current_text = session.dialog.draft.toPlainText()
            has_instructions = bool(session.dialog.instructions.toPlainText().strip())
            if session.dialog.sending or has_instructions or current_text != prior_text:
                self.chat_dialog.show_error("已保留回复窗口中的草稿，本次建议可在对话中查看。")
                return
            session.dialog.draft.setPlainText(draft.text)
        elif saved and (saved.instructions.strip() or saved.text != prior_text):
            self.chat_dialog.show_error("已保留草稿箱中的版本，本次建议可在对话中查看。")
            return
        elif key in self.sent_cleanup or (prior_text and not saved):
            self.chat_dialog.show_error("原草稿已移出草稿箱，本次建议可在对话中查看。")
            return
        else:
            self.saved_drafts[key] = draft
        self.dirty_drafts.add(key)
        self._save_edited_draft(key)
        if key in self.draft_errors:
            self.chat_dialog.show_error(self.draft_errors[key])

    def _open_chat_draft(self, item, text):
        chat = self.chat_dialog
        account = next((a for a in self.accounts if a.address == item.account), None)
        if chat is None or self.reply_model is None or account is None:
            return
        # Carry only exchanges about this message, up through the chosen draft card.
        key = _key(item)
        session = self.reply_drafts.get(key)
        saved = self.saved_drafts.get(key)
        context_text = saved.text if saved else text
        if session:
            context_text = session.dialog.draft.toPlainText() or text
        history = []
        for turn in chat.history:
            if turn.sources and all(_key(source) == key for source in turn.sources):
                history.append(ComposeTurn(turn.question, turn.answer, turn.draft_text or None))
                if turn.draft_item and _key(turn.draft_item) == key and turn.draft_text == context_text:
                    break
        history = tuple(history[-6:])
        if self._resume_reply(item, text, history):
            return
        # A user can deliberately reopen an earlier card after discarding its local draft.
        record = next((turn.draft_record for turn in reversed(chat.history)
                       if turn.draft_record and _key(turn.draft_record.item) == key and turn.draft_text == text), None)
        if record:
            self._create_reply(account, record.item, record.envelope, record.original_text, text, history)

    def _open_model_settings(self):
        if self.model_settings is not None:
            self.model_settings.show()
            self.model_settings.raise_()
            self.model_settings.activateWindow()
            return
        dialog = ModelSettingsDialog(self, self.reply_choice, self.reply_prefs, self._run)
        self.model_settings = dialog

        def finished(result):
            if result == QDialog.DialogCode.Accepted:
                self.reply_choice = dialog.choice
                self.reply_model = selected_model(dialog.choice, self.reply_prefs)
                self._update_chat()
                for draft in self.reply_drafts.values():
                    ai = self.reply_model
                    converse = partial(compose_turn, ai, draft.subject, draft.original_text) if ai else None
                    writer = partial(compose_draft, ai, draft.subject, draft.original_text) if ai else None
                    draft.dialog.set_model(ai.description if ai else "", None, converse=converse, draft_with=writer,
                                           complete=model_completer(ai, draft.subject, draft.original_text,
                                                                    draft.envelope.recipient if draft.envelope else ""))
            elif dialog.error.isVisibleTo(dialog):
                self.statusBar().showMessage(dialog.error.text())
            self.model_settings = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def _resume_reply(self, item, suggested="", history=()):
        key = _key(item)
        if key in self.sent_cleanup and not self._delete_draft_record(item):
            return True
        previous = self.reply_drafts.get(key)
        if previous is None:
            saved = self.saved_drafts.get(key)
            account = next((a for a in self.accounts if a.address == item.account), None)
            if not saved or not account:
                return False
            try:
                original = saved.original_text or self.service.original_for_draft(saved.item)
            except Exception as exc:
                self.statusBar().showMessage(f"暂时无法打开草稿：{exc}")
                return True
            self._create_reply(account, saved.item, saved.envelope, original, saved=saved)
            previous = self.reply_drafts[key]
        dialog = previous.dialog
        if suggested and not dialog.sending:
            dialog.restore_conversation(history)
            if dialog.draft.document().isEmpty():
                dialog.draft.setPlainText(suggested)
            elif dialog.draft.toPlainText() != suggested:
                dialog.status.setText("已保留原有草稿，助手的建议可在邮箱对话里复制。")
                dialog.status.show()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def _open_reply(self):
        item = self.reading_item
        if item is None or not self.body_ready or self._resume_reply(item):
            return
        account = next((account for account in self.accounts if account.address == item.account), None)
        if account is not None:
            self._create_reply(account, item, self.reply_envelope, self.body.toPlainText())

    def _set_model_completions(self, enabled):
        self.model_completions = enabled
        self.reply_prefs.setValue("modelCompletions", enabled)
        for draft in self.reply_drafts.values():
            draft.dialog.model_completion.setChecked(enabled)

    def _create_reply(self, account, item, envelope, text, suggested="", history=(), *, saved=None):
        key = _key(item)
        # Bind both actions to this message. The inbox can change under a modeless draft.
        ai = self.reply_model
        dialog = ReplyDialog(
            self, account=account.address, recipient=envelope.recipient if envelope else "",
            subject=envelope.subject if envelope else item.subject,
            model_description=ai.description if ai else "",
            suggestions=reply_suggestions(item.subject, text, envelope.recipient if envelope else ""),
            complete=model_completer(ai, item.subject, text, envelope.recipient if envelope else ""),
            draft_with=partial(compose_draft, ai, item.subject, text) if ai else None,
            converse=partial(compose_turn, ai, item.subject, text) if ai else None,
            send=partial(self.service.send_reply, account, item, envelope) if envelope else None,
            run=self._run,
        )
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.setInterval(350)
        timer.timeout.connect(partial(self._save_edited_draft, key))
        self.reply_drafts[key] = _ReplyDraft(dialog, item, envelope, text, timer)
        if saved:
            dialog.draft.setPlainText(saved.text)
            dialog.instructions.setPlainText(saved.instructions)

        def edited():
            self.dirty_drafts.add(key)
            timer.start()

        dialog.draft.textChanged.connect(edited)
        dialog.instructions.textChanged.connect(edited)
        if suggested:
            dialog.draft.setPlainText(suggested)
            dialog.restore_conversation(history)
        dialog.configure_requested.connect(self._open_model_settings)
        dialog.model_completion.setChecked(self.model_completions)
        dialog.model_completion_toggled.connect(self._set_model_completions)

        def finished(result):
            if self.closing or key not in self.reply_drafts:
                return
            sent = result == QDialog.DialogCode.Accepted and envelope is not None
            if sent:
                timer.stop()
                self.dirty_drafts.discard(key)
                self.saved_drafts.pop(key, None)
                self.sent_cleanup[key] = item
                cleared = self._delete_draft_record(item)
                confirmation = "发件服务器已接收回复"
                if not cleared:
                    confirmation += "（本机草稿待清理）"
                self.statusBar().showMessage(f"{confirmation} · {envelope.recipient}", 6500)
                self._draft_changed(key)
            elif key in self.dirty_drafts:
                self._save_edited_draft(key)
            if sent or (not dialog.has_session_content and key not in self.dirty_drafts):
                self.reply_drafts.pop(key, None)
                dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _load_external_images(self, *, retry=False):
        body_id = self.body_id
        item = self.reading_item
        stop = self.image_download_stop
        pending = iter(self.body.remote_urls)
        self.reader.image_notice.hide()

        def load_next():
            urls = tuple(islice(pending, IMAGE_BATCH_SIZE))
            if not urls:
                self.reader.show_image_failures()
                return

            def done(images, error):
                if body_id != self.body_id:
                    return
                self.body.set_remote_images({} if error else images, attempted_urls=urls)
                self.copy_button.setEnabled(bool(self.body.toPlainText().replace("\ufffc", "").strip()))
                load_next()

            self._run(lambda: self.service.read_images(item, urls, download_images, retry=retry,
                                                      cancelled=stop.is_set), done)

        load_next()

    def _activate_row(self, row):
        if self.nav_kind == "drafts":
            self._resume_reply(row.data(USER_ROLE))

    def _sync_seen(self):
        item = self.reading_item
        if item is None or not self.body_ready or item.unread is False:
            return
        account = next((a for a in self.accounts if a.address == item.account), None)
        if account is None:
            return
        key = _key(item)
        self.reader.set_seen_status("正在同步已读状态…")
        if key in self.marking_read:
            return
        self.marking_read.add(key)

        def done(_, error):
            self.marking_read.discard(key)
            is_open = self.nav_kind != "drafts" and self.reading_item is not None and _key(self.reading_item) == key
            if error:
                if is_open and self.body_ready:
                    self._track_authorization(account.address, error)
                    self._list_state()
                    self.reader.set_seen_status(f"已读状态同步失败：{error}", retry=True)
                return
            # A FETCH started before STORE can arrive with the old flags.
            if account.address in self.seen_during_page_load:
                self.seen_during_page_load[account.address].add(key)
            for cache in (self.items, self.queue_items):
                if key in cache:
                    cache[key] = replace(cache[key], unread=False)
            flags = self.mailbox_flags.get(account.address)
            if flags:
                flags.unread_keys.discard(key)
            if is_open:
                self.reading_item = replace(self.reading_item, unread=False)
                self.reader.set_seen_status()
            self._render_rows(keep_reading=key)
            self._list_state()

        self._run(lambda: self.service.mark_read(account, item), done)

    def _update_navigation(self):
        row = self.mail_list.currentRow()
        self.previous_button.setEnabled(row > 0)
        self.next_button.setEnabled(row + 1 < self.mail_list.count())
        current = self.mail_list.currentItem()
        self.discard_button.setEnabled(self.nav_kind == "drafts" and current is not None)
        complete = self.nav_kind == "pending" and current is not None
        self.reader.complete.setVisible(complete)
        if complete:
            self.reader.complete.setEnabled(_key(current.data(USER_ROLE)) not in self.finishing)

    def _mail_changed(self, row, previous=None):
        self.image_download_stop.set()
        self.image_download_stop = Event()
        self.body_id += 1
        body_id = self.body_id
        self.body_ready = False
        self.reply_envelope = None
        self.reply_button.setEnabled(False)
        self.reading_item = row.data(USER_ROLE) if row is not None else None
        self._update_navigation()
        self.copy_button.setEnabled(False)
        if self.reading_item is None:
            self.reader.clear()
            return
        item: MailItem = self.reading_item
        if self.nav_kind == "drafts":
            self._show_draft(self.saved_drafts[_key(item)])
            return
        account = next((a for a in self.accounts if a.address == item.account), None)
        if account is None:
            self.reader.clear()
            return
        self.reader.show_item(item)

        def done(content, error):
            if body_id != self.body_id:
                return
            self.body_ready = not error
            if error:
                self._track_authorization(account.address, error)
                self._list_state()
                self.reader.set_body(f"正文读取失败：{error}", error=True)
                return
            self.reader.set_content(content)
            self.reply_envelope = content.reply
            self.reply_button.setEnabled(True)
            self.copy_button.setEnabled(bool(self.body.toPlainText().replace("\ufffc", "").strip()))
            self._sync_seen()
            self._load_external_images()

        self._run(lambda: self.service.read_content(account, item), done, stop=self.stop_sync)
