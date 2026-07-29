"""通用 UI 反馈辅助。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QWidget

from ui.dialogs.alert_dialog import AlertDialog, ConfirmDialog


def show_alert(parent: QWidget | None, title: str, message: str) -> None:
    dlg = AlertDialog(title, message, parent.window() if parent else None)
    dlg.exec()


def ask_confirmation(parent: QWidget | None, title: str, message: str) -> bool:
    dlg = ConfirmDialog(title, message, parent.window() if parent else None)
    return dlg.exec() == ConfirmDialog.Accepted


def ask_confirmation_sequence(
    parent: QWidget | None,
    prompts: Sequence[tuple[str, str]],
) -> bool:
    for title, message in prompts:
        if not ask_confirmation(parent, title, message):
            return False
    return True
