"""Account network work kept off the GUI thread."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.auth_client import AuthClient, AuthSession


class LoginWorker(QThread):
    completed = Signal(object, str)

    def __init__(self, client: AuthClient, username: str, password: str, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.username = username
        self.password = password

    def run(self) -> None:
        try:
            self.completed.emit(self.client.login(self.username, self.password), "")
        except Exception as exc:
            self.completed.emit(None, str(exc))


class SessionValidationWorker(QThread):
    completed = Signal(object, str)

    def __init__(self, client: AuthClient, session: AuthSession, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.session = session

    def run(self) -> None:
        try:
            self.completed.emit(self.client.validate_session(self.session), "")
        except Exception as exc:
            self.completed.emit(self.session, str(exc))


class LogoutWorker(QThread):
    completed = Signal()

    def __init__(self, client: AuthClient, session: AuthSession, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.session = session

    def run(self) -> None:
        self.client.logout(self.session)
        self.completed.emit()
