"""通用 UI 反馈辅助。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QWidget

from ui.dialogs.alert_dialog import AlertDialog, ConfirmDialog


def show_alert(parent: QWidget | None, title: str, message: str) -> None:
    dlg = AlertDialog(title, message, parent.window() if parent else None)
    dlg.exec()


def ask_confirmation(
    parent: QWidget | None,
    title: str,
    message: str,
    *,
    box_width: int | None = None,
    warning_text: str = "",
    acknowledgement_text: str = "",
    ok_text: str = "确定",
) -> bool:
    kwargs: dict = {"ok_text": ok_text}
    if box_width is not None:
        kwargs["box_width"] = box_width
    if warning_text:
        kwargs["warning_text"] = warning_text
    if acknowledgement_text:
        kwargs["acknowledgement_text"] = acknowledgement_text
    dlg = ConfirmDialog(title, message, parent.window() if parent else None, **kwargs)
    return dlg.exec() == ConfirmDialog.Accepted


def ask_confirmation_sequence(
    parent: QWidget | None,
    prompts: Sequence[tuple[str, str]],
) -> bool:
    for title, message in prompts:
        if not ask_confirmation(parent, title, message):
            return False
    return True
