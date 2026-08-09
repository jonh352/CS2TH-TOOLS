"""Logged-in account card: entitlements summary + logout."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class AccountDialog(QDialog):
    """Frameless account panel matching the login dialog look."""

    def __init__(
        self,
        *,
        username: str,
        entitlement_lines: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("账号")
        self.setModal(True)
        self.setObjectName("loginDialog")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(400)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("loginCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 14)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)
        root.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        title = QLabel("账号")
        title.setObjectName("loginTitle")
        close_button = QPushButton("×")
        close_button.setObjectName("loginCloseButton")
        close_button.setFixedSize(28, 28)
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(close_button)
        layout.addLayout(heading)

        name = QLabel(str(username or "").strip() or "未命名用户")
        name.setObjectName("accountDialogUsername")
        name.setWordWrap(True)
        layout.addWidget(name)

        hint = QLabel("当前有效权益")
        hint.setObjectName("loginHint")
        layout.addWidget(hint)

        entitlements = QFrame()
        entitlements.setObjectName("accountDialogEntitlementBox")
        ent_lay = QVBoxLayout(entitlements)
        ent_lay.setContentsMargins(12, 10, 12, 10)
        ent_lay.setSpacing(8)
        lines = [str(line).strip() for line in entitlement_lines if str(line).strip()]
        if lines:
            for line in lines:
                row = QLabel(f"·  {line}")
                row.setObjectName("accountDialogEntitlementRow")
                row.setWordWrap(True)
                ent_lay.addWidget(row)
        else:
            empty = QLabel("暂无有效会员权益")
            empty.setObjectName("accountDialogEntitlementEmpty")
            empty.setWordWrap(True)
            ent_lay.addWidget(empty)
        layout.addWidget(entitlements)
        layout.addSpacing(4)

        logout = QPushButton("退出登录")
        logout.setObjectName("accountDialogLogoutButton")
        logout.setCursor(Qt.CursorShape.PointingHandCursor)
        logout.clicked.connect(self.accept)
        layout.addWidget(logout)

        stay = QPushButton("继续使用")
        stay.setObjectName("accountDialogStayButton")
        stay.setCursor(Qt.CursorShape.PointingHandCursor)
        stay.clicked.connect(self.reject)
        layout.addWidget(stay)
