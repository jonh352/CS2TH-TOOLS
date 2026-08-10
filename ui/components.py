"""Small shared widgets; page-specific behavior stays in each page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import BRAND_IMAGE


def panel(parent: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
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


class AuthBrandHeader(QWidget):
    """Auth header: centered logo + CS2TH, close pinned top-right."""

    def __init__(
        self,
        caption: str = "",
        *,
        on_close=None,
        parent: QWidget | None = None,
        logo_size: int = 40,
    ) -> None:
        super().__init__(parent)
        _ = caption
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # Mirror close width so brand mark stays optically centered.
        balance = QWidget()
        balance.setFixedSize(28, 28)
        row.addWidget(balance, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        brand = QHBoxLayout()
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(8)
        logo = QLabel()
        logo.setObjectName("authBrandLogo")
        logo.setFixedSize(logo_size, logo_size)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if BRAND_IMAGE.is_file():
            pixmap = QPixmap(str(BRAND_IMAGE))
            logo.setPixmap(
                pixmap.scaled(
                    logo_size,
                    logo_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("CS2TH")
        title.setObjectName("authBrandName")
        brand.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(brand)

        row.addStretch(1)
        close_button = QPushButton("×")
        close_button.setObjectName("loginCloseButton")
        close_button.setFixedSize(28, 28)
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        if on_close is not None:
            close_button.clicked.connect(on_close)
        row.addWidget(close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close_button = close_button
