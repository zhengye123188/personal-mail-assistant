"""Elect one desktop process per data directory and forward requests to open its window."""

import hashlib
import os
from pathlib import Path
from time import monotonic, sleep

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class MailInstance(QObject):
    activation_requested = Signal()

    def __init__(self, data_dir: Path):
        super().__init__()
        directory = data_dir.resolve()
        key = hashlib.sha256(os.fsencode(os.path.normcase(str(directory)))).hexdigest()[:32]
        self._name = f"personal-mail-{key}"
        self._lock = QLockFile(str(directory / "desktop.lock"))
        # A running mailbox can keep its lock for days. Qt still checks whether its owner died.
        self._lock.setStaleLockTime(0)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._activate)

    def acquire_or_activate(self) -> bool:
        """True for the owner. A second launch exits once its activation is queued."""
        deadline = monotonic() + 3
        while not self._lock.tryLock(0):
            if self._lock.error() != QLockFile.LockError.LockFailedError:
                raise RuntimeError("无法在应用数据目录创建启动锁。")
            peer = QLocalSocket()
            peer.connectToServer(self._name)
            if peer.waitForConnected(120):
                peer.disconnectFromServer()
                return False
            if monotonic() >= deadline:
                raise RuntimeError("邮件助手正在运行，但窗口暂时未响应。请稍后重试。")
            sleep(0.04)  # A simultaneous first launch may be between lock and listen.

        # Only the elected owner may remove a socket left after an abnormal exit.
        QLocalServer.removeServer(self._name)
        if not self._server.listen(self._name):
            self._lock.unlock()
            raise RuntimeError(f"无法启动本机窗口通信：{self._server.errorString()}")
        return True

    def _activate(self):
        while self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            client.close()
            client.deleteLater()
        self.activation_requested.emit()

    def close(self):
        self._server.close()
        if self._lock.isLocked():
            self._lock.unlock()
