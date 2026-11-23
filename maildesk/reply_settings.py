"""Opt-in reply models. Credentials are kept in the system credential store."""

import os
from functools import partial

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
    QWidget,
)

from .reply_ai import KEY_ENV, MODELS, PROVIDER_NAMES, DemoReplyAI, ReplyAI
from .service import KeyringVault


OFF = ("off", "")
_KEY_PAGES = {"deepseek": "https://platform.deepseek.com/api_keys",
              "dashscope": "https://help.aliyun.com/zh/model-studio/get-api-key"}


def saved_choice(settings):
    if settings is None:
        return OFF
    choice = settings.value("selection", {})
    if isinstance(choice, dict):
        provider, model = choice.get("provider"), choice.get("model")
        if provider in KEY_ENV and model in dict(MODELS[provider]):
            return provider, model
    return OFF


def selected_model(choice, settings):
    provider, model = choice
    if provider == "off":
        return None
    if settings is None:
        return DemoReplyAI()
    lookup = None
    if settings.value(f"keys/{provider}", False, type=bool):
        lookup = partial(KeyringVault().get, f"reply-api/{provider}")
    return ReplyAI(provider=provider, model=model, key_lookup=lookup)


class ModelSettingsDialog(QDialog):
    def __init__(self, parent, choice, settings, run):
        super().__init__(parent)
        self.settings = settings
        self.run = run
        self.choice = choice
        self.busy = False
        self.setWindowTitle("AI 设置")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        self.enabled = QCheckBox("启用邮箱 AI 助手")
        self.enabled.setChecked(choice != OFF)
        layout.addWidget(self.enabled)
        self.fields = QWidget()
        form = QFormLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        self.provider = QComboBox()
        for provider in KEY_ENV:
            self.provider.addItem(PROVIDER_NAMES[provider], provider)
        form.addRow("服务商", self.provider)
        self.model = QComboBox()
        form.addRow("模型", self.model)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setMaxLength(512)
        form.addRow("API 密钥", self.key)
        form.setRowVisible(self.key, settings is not None)
        self.key_page = QLabel()
        self.key_page.setOpenExternalLinks(True)
        form.addRow("", self.key_page)
        form.setRowVisible(self.key_page, settings is not None)
        layout.addWidget(self.fields)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.hint)
        self.error = QLabel()
        self.error.setStyleSheet("color: #ad3737")
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.hide()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        self.provider.setCurrentIndex(max(0, self.provider.findData(choice[0])))
        self.provider.currentIndexChanged.connect(self._show_provider)
        self.enabled.toggled.connect(self._show_mode)
        self._show_provider()
        self._show_mode()

    def _show_provider(self):
        provider = self.provider.currentData()
        self.key.clear()
        self.model.clear()
        for model, label in MODELS[provider]:
            self.model.addItem(label, model)
        if self.choice[0] == provider:
            self.model.setCurrentIndex(max(0, self.model.findData(self.choice[1])))
        saved = self.settings is not None and self.settings.value(f"keys/{provider}", False, type=bool)
        self.key.setPlaceholderText("已保存，留空继续使用" if saved else f"输入 {PROVIDER_NAMES[provider]} API 密钥")
        self.key_page.setText(f'<a href="{_KEY_PAGES[provider]}">获取 {PROVIDER_NAMES[provider]} API 密钥</a>')
        self._show_mode()

    def _show_mode(self):
        enabled = self.enabled.isChecked()
        self.fields.setVisible(enabled)
        if not enabled:
            text = "普通邮箱模式，可直接阅读、撰写和发送回复。"
        elif self.settings is None:
            text = "离线演示使用示例邮箱，可以查信、询问来信内容、修改草稿和模拟发送。"
        else:
            name = PROVIDER_NAMES[self.provider.currentData()]
            text = (f"提问时，{name} 会收到问题、相关对话，以及所选邮箱中相关邮件的主题和正文节选。"
                    "在回复框里提问也会带上正在编辑的草稿。API 密钥保存在系统凭据库。")
        self.hint.setText(text)
        self.error.hide()

    def _save(self):
        if self.busy:
            return
        provider = self.provider.currentData() if self.enabled.isChecked() else "off"
        model = self.model.currentData() if provider != "off" else ""
        key = self.key.text().strip() if provider != "off" and self.settings is not None else ""
        if key and (not key.isascii() or not key.isprintable() or " " in key):
            self._show_error("请检查 API 密钥中是否含有空格或非英文字符。")
            return
        if (self.settings is not None and provider != "off" and not key
                and not os.getenv(KEY_ENV[provider]) and not self.settings.value(f"keys/{provider}", False, type=bool)):
            self._show_error(f"请输入{PROVIDER_NAMES[provider]}的 API 密钥。")
            return

        def finish(_, error):
            self.busy = False
            self.setEnabled(True)
            if error:
                self._show_error("系统凭据库未能保存密钥，请解锁凭据库后重试。")
                return
            if self.settings is not None:
                if key:
                    self.settings.setValue(f"keys/{provider}", True)
                self.settings.setValue("selection", {"provider": provider, "model": model})
                self.settings.sync()
                if self.settings.status() != QSettings.Status.NoError:
                    self._show_error("无法保存模型设置，请检查本机设置目录的写入权限。")
                    return
            self.choice = provider, model
            self.key.clear()
            self.accept()

        if key:
            self.busy = True
            self.setEnabled(False)
            self.run(lambda: KeyringVault().set(f"reply-api/{provider}", key), finish)
        else:
            finish(None, None)

    def _show_error(self, message):
        self.error.setText(message)
        self.error.show()

    def reject(self):
        if not self.busy:
            self.key.clear()
            super().reject()
