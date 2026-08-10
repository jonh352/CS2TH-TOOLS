"""CS2TH account dialog matching the shared desktop design."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.auth_client import AuthClient, AuthSession
from ui.components import AuthBrandHeader
from ui.dialogs.information_dialogs import show_information_dialog
from ui.workers.auth import LoginWorker


class LoginDialog(QDialog):
    logged_in = Signal(object)

    def __init__(self, client: AuthClient, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.worker: LoginWorker | None = None
        self._backdrop: QWidget | None = None
        self.setWindowTitle("登录 CS2TH")
        self.setModal(True)
        self.setObjectName("loginDialog")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(400)

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
        layout.setContentsMargins(22, 14, 22, 18)
        layout.setSpacing(9)

        layout.addWidget(AuthBrandHeader(on_close=self.reject, logo_size=40))
        layout.addSpacing(6)

        heading = QLabel("用户登录")
        heading.setObjectName("loginFormHeading")
        layout.addWidget(heading)
        layout.addSpacing(2)

        self.username = QLineEdit()
        self.username.setObjectName("loginField")
        self.username.setPlaceholderText("用户名")
        self.username.setClearButtonEnabled(True)
        layout.addWidget(self.username)

        self.password = QLineEdit()
        self.password.setObjectName("loginField")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("密码")
        layout.addWidget(self.password)

        forgot = QLabel('<a href="https://cs2th.cn/?login=1&reset=1">忘记密码？</a>')
        forgot.setObjectName("loginLink")
        forgot.setAlignment(Qt.AlignmentFlag.AlignRight)
        forgot.setOpenExternalLinks(True)
        layout.addWidget(forgot)

        self.message = QLabel()
        self.message.setObjectName("loginMessage")
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)

        self.submit = QPushButton("登录")
        self.submit.setObjectName("loginSubmitButton")
        self.submit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit.clicked.connect(self._login)
        layout.addWidget(self.submit)

        consent = QLabel(
            '登录即代表同意 <a href="agreement">《用户协议》</a> 和'
            ' <a href="privacy">《隐私政策》</a>'
        )
        consent.setObjectName("loginFooter")
        consent.setAlignment(Qt.AlignmentFlag.AlignCenter)
        consent.setWordWrap(True)
        consent.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        consent.setOpenExternalLinks(False)
        consent.linkActivated.connect(self._open_legal)
        layout.addWidget(consent)

        register = QLabel(
            '没有账号？<a href="https://cs2th.cn/?login=1&register=1">注册</a>'
        )
        register.setObjectName("loginFooter")
        register.setAlignment(Qt.AlignmentFlag.AlignCenter)
        register.setOpenExternalLinks(True)
        layout.addWidget(register)

        self.username.returnPressed.connect(self.password.setFocus)
        self.password.returnPressed.connect(self._login)
        self.username.setFocus()

    def showEvent(self, event) -> None:
        parent = self.parentWidget()
        if parent is not None and self._backdrop is None:
            self._backdrop = QWidget(parent)
            self._backdrop.setObjectName("loginBackdrop")
            self._backdrop.setGeometry(parent.rect())
            self._backdrop.show()
            self._backdrop.raise_()
        super().showEvent(event)

    def done(self, result: int) -> None:
        if self._backdrop is not None:
            self._backdrop.deleteLater()
            self._backdrop = None
        super().done(result)

    def _open_legal(self, link: str) -> None:
        page = "用户协议" if link == "agreement" else "隐私政策"
        show_information_dialog(self, page)

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setProperty("error", error)
        self.message.setVisible(bool(text))
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def _login(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self._set_message("请输入用户名和密码", error=True)
            return
        self.submit.setEnabled(False)
        self._set_message("正在登录…")
        self.worker = LoginWorker(self.client, username, password, self)
        self.worker.completed.connect(self._finished)
        self.worker.start()

    def _finished(self, session: AuthSession | None, error: str) -> None:
        self.submit.setEnabled(True)
        if error:
            self._set_message(error, error=True)
            return
        self.logged_in.emit(session)
        self.accept()
