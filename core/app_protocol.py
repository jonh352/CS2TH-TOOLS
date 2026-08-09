"""Single-instance messaging and the cs2th-tools URL protocol."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


APP_PROTOCOL = "cs2th-tools"
INSTANCE_SERVER_NAME = "CS2TH.Tools.Primary.v1"
FOCUS_COMMAND = "focus"
_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def recipe_reference_from_command(command: str) -> str:
    """Return the CS2TH recipe URL carried by a protocol command."""
    parsed = urlparse(str(command or "").strip())
    if parsed.scheme.lower() != APP_PROTOCOL or parsed.netloc.lower() != "import-recipe":
        raise ValueError("不支持的小助手链接")
    reference = (parse_qs(parsed.query).get("url") or [""])[0].strip()
    if not reference:
        raise ValueError("小助手链接中缺少配方地址")
    recipe = urlparse(reference)
    if recipe.scheme not in {"http", "https"} or recipe.hostname not in {
        "cs2th.cn",
        "www.cs2th.cn",
    }:
        raise ValueError("仅支持导入 cs2th.cn 的配方")
    parts = [part for part in recipe.path.split("/") if part]
    if (
        len(parts) != 2
        or parts[0] != "recipe"
        or not _RECIPE_ID_RE.fullmatch(parts[1])
    ):
        raise ValueError("配方地址格式不正确")
    return reference


def protocol_command_from_argv(arguments: list[str]) -> str:
    for argument in arguments[1:]:
        value = str(argument or "").strip()
        if value.lower().startswith(f"{APP_PROTOCOL}://"):
            return value
    return ""


def send_to_running_instance(command: str, timeout_ms: int = 900) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        return False
    payload = command.encode("utf-8")
    if socket.write(payload) != len(payload):
        return False
    socket.flush()
    if socket.bytesToWrite() > 0 and not socket.waitForBytesWritten(timeout_ms):
        return False
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(timeout_ms)
    return True


class SingleInstanceServer(QObject):
    """Receive commands from subsequent launches on the current user's session."""

    command_received = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)

    def listen(self) -> bool:
        if self._server.listen(INSTANCE_SERVER_NAME):
            return True
        QLocalServer.removeServer(INSTANCE_SERVER_NAME)
        return self._server.listen(INSTANCE_SERVER_NAME)

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.readyRead.connect(lambda peer=socket: self._read_command(peer))
            socket.disconnected.connect(socket.deleteLater)

    def _read_command(self, socket: QLocalSocket) -> None:
        command = bytes(socket.readAll()).decode("utf-8", errors="replace").strip()
        if command:
            self.command_received.emit(command)
