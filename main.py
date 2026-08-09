"""CS2TH 汰换小助手 application entry point."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from config import APP_ICON, APP_NAME, APP_VERSION, ensure_runtime_dirs
from core.app_protocol import (
    FOCUS_COMMAND,
    SingleInstanceServer,
    protocol_command_from_argv,
    send_to_running_instance,
)
from ui.main_window import MainWindow


def _set_windows_app_user_model_id(app_id: str) -> None:
    """让任务栏使用本应用图标，而不是 python.exe。须在创建 QApplication 之前调用。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def main() -> int:
    # PyInstaller 打包后炼金多进程需要
    import multiprocessing

    multiprocessing.freeze_support()
    ensure_runtime_dirs()
    _set_windows_app_user_model_id("CS2TH.Tools")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("CS2TH")
    app.setStyle("Fusion")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    protocol_command = protocol_command_from_argv(sys.argv)
    forwarded_command = protocol_command or FOCUS_COMMAND
    if send_to_running_instance(forwarded_command):
        return 0
    instance_server = SingleInstanceServer(app)
    if not instance_server.listen():
        QMessageBox.warning(None, APP_NAME, "小助手启动失败：无法建立单实例通信。")
        return 1
    window = MainWindow()
    instance_server.command_received.connect(window.handle_external_command)
    window.show()
    if protocol_command:
        QTimer.singleShot(0, lambda: window.handle_external_command(protocol_command))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
