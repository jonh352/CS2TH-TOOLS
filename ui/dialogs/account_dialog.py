"""Logged-in account card: entitlements summary + logout."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.components import AuthBrandHeader


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
        self.setFixedWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("loginCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 135))
        card.setGraphicsEffect(shadow)
        root.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 14, 22, 16)
        layout.setSpacing(9)

        layout.addWidget(AuthBrandHeader(on_close=self.reject, logo_size=40))
        layout.addSpacing(4)

        user_heading = QLabel("用户名")
        user_heading.setObjectName("loginFormHeading")
        layout.addWidget(user_heading)

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
        ent_lay.setContentsMargins(10, 8, 10, 8)
        ent_lay.setSpacing(4)
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
