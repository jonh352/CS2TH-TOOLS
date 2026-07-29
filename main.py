"""CS2TH 汰换小助手 application entry point."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from config import APP_ICON, APP_NAME, APP_VERSION, ensure_runtime_dirs
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
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
