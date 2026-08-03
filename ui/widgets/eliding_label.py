"""Single-line QLabel that elides overflowing text with an ellipsis."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidingLabel(QLabel):
    """Keep layout width stable; show ``...`` when the full text does not fit."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setWordWrap(False)
        self.setText(text)

    def text(self) -> str:  # type: ignore[override]
        return self._full_text

    def setText(self, text: str | None) -> None:  # type: ignore[override]
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._apply_elide()

    def minimumSizeHint(self):  # type: ignore[override]
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self):  # type: ignore[override]
        hint = super().sizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = max(0, self.width() - 2 * self.margin() - 4)
        elided = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            width,
        )
        QLabel.setText(self, elided)
