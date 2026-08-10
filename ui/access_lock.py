"""Browse-only mode: keep pages visible while blocking interactive controls."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSlider,
    QTextEdit,
    QWidget,
)

_PREV_PROP = "cs2thPrevEnabled"
_INTERACTIVE_TYPES = (
    QAbstractButton,
    QLineEdit,
    QAbstractSpinBox,
    QComboBox,
    QAbstractItemView,
    QSlider,
    QTextEdit,
    QPlainTextEdit,
)


def apply_page_interaction_lock(root: QWidget | None, locked: bool) -> None:
    """Disable/restore interactive children so locked users can still look around."""
    if root is None:
        return
    for widget in root.findChildren(QWidget):
        if not isinstance(widget, _INTERACTIVE_TYPES):
            continue
        if locked:
            if widget.property(_PREV_PROP) is None:
                widget.setProperty(_PREV_PROP, widget.isEnabled())
            widget.setEnabled(False)
            continue
        previous = widget.property(_PREV_PROP)
        if previous is None:
            continue
        widget.setEnabled(bool(previous))
        widget.setProperty(_PREV_PROP, None)
        # Force QSS to refresh enabled/disabled visuals.
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
