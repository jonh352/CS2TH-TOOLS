import os
import subprocess
import sys
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalServer

import core.app_protocol as app_protocol
from core.app_protocol import (
    SingleInstanceServer,
    protocol_command_from_argv,
    recipe_reference_from_command,
)


class AppProtocolTests(unittest.TestCase):
    def test_recipe_reference_from_protocol_command(self) -> None:
        recipe_url = "https://cs2th.cn/recipe/4a40afd825ee0000?market=spot"
        command = f"cs2th-tools://import-recipe?url={quote(recipe_url, safe='')}"
        self.assertEqual(recipe_reference_from_command(command), recipe_url)

    def test_recipe_reference_rejects_invalid_commands(self) -> None:
        commands = [
            "https://cs2th.cn/recipe/4a40afd825ee0000",
            "cs2th-tools://unknown?url=https%3A%2F%2Fcs2th.cn%2Frecipe%2F4a40afd825ee0000",
            "cs2th-tools://import-recipe?url=https%3A%2F%2Fexample.com%2Frecipe%2F4a40afd825ee0000",
            "cs2th-tools://import-recipe?url=https%3A%2F%2Fcs2th.cn%2Fplaza%2Fspot",
            "cs2th-tools://import-recipe?url=https%3A%2F%2Fcs2th.cn%2Frecipe%2Fbad",
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                recipe_reference_from_command(command)

    def test_protocol_command_from_argv_ignores_other_arguments(self) -> None:
        command = "cs2th-tools://import-recipe?url=x"
        self.assertEqual(protocol_command_from_argv(["app.exe", "--flag", command]), command)
        self.assertEqual(protocol_command_from_argv(["app.exe", "--flag"]), "")

    def test_single_instance_server_receives_forwarded_command(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        server_name = f"CS2TH.Tools.Test.{os.getpid()}"
        received: list[str] = []
        sent: list[bool] = []
        server = SingleInstanceServer()
        command = "cs2th-tools://import-recipe?url=test"

        def record(value: str) -> None:
            received.append(value)
            app.quit()

        def send() -> None:
            code = (
                "import core.app_protocol as p; "
                f"p.INSTANCE_SERVER_NAME={server_name!r}; "
                f"raise SystemExit(0 if p.send_to_running_instance({command!r}, 1500) else 1)"
            )
            result = subprocess.run([sys.executable, "-c", code], check=False)
            sent.append(result.returncode == 0)

        server.command_received.connect(record)
        with patch.object(app_protocol, "INSTANCE_SERVER_NAME", server_name):
            self.assertTrue(server.listen())
            worker = threading.Thread(target=send, daemon=True)
            QTimer.singleShot(0, worker.start)
            QTimer.singleShot(3_000, app.quit)
            app.exec()
            worker.join(timeout=1)
            server._server.close()
            QLocalServer.removeServer(server_name)

        self.assertEqual(sent, [True])
        self.assertEqual(received, [command])


if __name__ == "__main__":
    unittest.main()
