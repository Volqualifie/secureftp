"""图形界面入口。

    python -m client.gui
"""
import sys

from PySide6.QtWidgets import QApplication, QDialog


def main() -> int:
    from .ui.login_dialog import LoginDialog
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("SecureFTP Client")

    dlg = LoginDialog()
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.client is None:
        return 0
    win = MainWindow(dlg.client, dlg.server_label)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())