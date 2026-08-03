"""Shared, comfortably sized single-line text input dialog."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QInputDialog, QLineEdit, QWidget


def create_wide_text_input_dialog(
    parent: QWidget | None,
    *,
    title: str,
    label: str,
    value: str = "",
) -> QInputDialog:
    """Create the standard text prompt used for links and saved file names."""
    dialog = QInputDialog(parent)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setTextEchoMode(QLineEdit.EchoMode.Normal)
    dialog.setTextValue(value)
    dialog.setOkButtonText("确定")
    dialog.setCancelButtonText("取消")
    dialog.setMinimumSize(520, 180)
    dialog.resize(560, 190)
    line_edit = dialog.findChild(QLineEdit)
    if line_edit is not None:
        line_edit.setMinimumWidth(460)
        line_edit.selectAll()
    return dialog


def get_wide_text_input(
    parent: QWidget | None,
    *,
    title: str,
    label: str,
    value: str = "",
) -> tuple[str, bool]:
    dialog = create_wide_text_input_dialog(
        parent,
        title=title,
        label=label,
        value=value,
    )
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.textValue(), accepted
