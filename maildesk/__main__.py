import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from .demo import DEMO_AVAILABLE
from .mail_client import MailClient
from .service import KeyringVault, MailService
from .ui import MainWindow


def main():
    parser = argparse.ArgumentParser(description="QQ 和网易 163 个人邮件助手")
    parser.set_defaults(demo=False)
    if DEMO_AVAILABLE:
        parser.add_argument("--demo", action="store_true", help="打开两个离线样例收件箱")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PersonalMailAssistant")
    app.setApplicationDisplayName("个人邮件助手")
    if args.demo:
        from .demo import DEMO_ACCOUNTS, DemoClient, DemoVault

        with TemporaryDirectory(prefix="maildesk-demo-") as folder:
            client = DemoClient()
            service = MailService(Path(folder) / "accounts.sqlite3", client, DemoVault())
            for account in DEMO_ACCOUNTS:
                service.bind(account.provider, account.address, "demo")
            window = MainWindow(service, demo=True, simulate_arrival=client.simulate_arrival)
            window.show()
            return app.exec()

    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    service = MailService(data_dir / "accounts.sqlite3", MailClient(), KeyringVault())
    window = MainWindow(service)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
