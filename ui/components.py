"""Small shared widgets; page-specific behavior stays in each page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


def panel(parent: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return frame, layout


class PageHeader(QWidget):
    def __init__(self, kicker: str, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        kicker_label = QLabel(kicker.upper())
        kicker_label.setObjectName("pageKicker")
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        layout.addWidget(kicker_label)
        layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("muted")
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

