"""Shared, comfortably sized single-line text input dialog (themed modal shell)."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ui.dialogs.alert_dialog import TextPromptDialog, prompt_text
from ui.modal_shell import MODAL_WIDTH_LG


def create_wide_text_input_dialog(
    parent: QWidget | None,
    *,
    title: str,
    label: str,
    value: str = "",
) -> TextPromptDialog:
    """Create the standard text prompt used for links and saved file names."""
    return TextPromptDialog(
        title,
        label,
        parent,
        default=value,
        box_width=MODAL_WIDTH_LG,
    )


def get_wide_text_input(
    parent: QWidget | None,
    *,
    title: str,
    label: str,
    value: str = "",
) -> tuple[str, bool]:
    return prompt_text(
        parent,
        title,
        label,
        default=value,
        box_width=MODAL_WIDTH_LG,
    )
