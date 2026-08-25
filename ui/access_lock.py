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


def _has_interactive_ancestor(widget: QWidget, root: QWidget) -> bool:
    """内嵌编辑器由其交互父控件统一锁定，不能重复保存启用状态。

    例如 ``QSpinBox`` 内部自带一个 ``QLineEdit``。禁用 SpinBox 后再读取
    该 LineEdit 的 ``isEnabled()`` 会得到 False；若把这个派生状态也保存，
    解锁时就会错误地把内部编辑器永久禁用。
    """
    parent = widget.parentWidget()
    while parent is not None and parent is not root:
        if isinstance(parent, _INTERACTIVE_TYPES):
            return True
        parent = parent.parentWidget()
    return False


def apply_page_interaction_lock(root: QWidget | None, locked: bool) -> None:
    """Disable/restore interactive children so locked users can still look around."""
    if root is None:
        return
    for widget in root.findChildren(QWidget):
        if not isinstance(widget, _INTERACTIVE_TYPES):
            continue
        if _has_interactive_ancestor(widget, root):
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
