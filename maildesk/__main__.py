import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import QApplication

from .mail_client import MailClient
from .service import KeyringVault, MailService
from .single_instance import MailInstance
from .ui import MainWindow


def main():
    parser = argparse.ArgumentParser(description="个人邮件助手 · 多账号收件箱")
    parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PersonalMailAssistant")
    app.setApplicationDisplayName("个人邮件助手")
    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = MailInstance(data_dir)
    try:
        primary = instance.acquire_or_activate()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    if not primary:
        return 0
    try:
        service = MailService(data_dir / "accounts.sqlite3", MailClient(), KeyringVault())
        window = MainWindow(service)

        def present():
            if window.isMinimized():
                window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
            window.show()
            target = app.activeModalWidget() or app.activeWindow() or window
            target.raise_()
            target.activateWindow()

        instance.activation_requested.connect(present)
        window.show()
        return app.exec()
    finally:
        instance.close()


if __name__ == "__main__":
    sys.exit(main())
