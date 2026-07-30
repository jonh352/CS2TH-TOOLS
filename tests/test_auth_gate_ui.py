from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.auth_client import Account, AuthSession
from ui.main_window import MainWindow


class MainWindowAccessGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.start_patch = patch.object(
            MainWindow, "_start_auth_validation"
        )
        self.activate_patch = patch.object(MainWindow, "_activate")
        self.start_patch.start()
        self.activate = self.activate_patch.start()
        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.activate_patch.stop()
        self.start_patch.stop()

    def test_guest_cannot_open_feature_pages(self) -> None:
        self.assertFalse(self.window._access_allowed)
        for key, button in self.window.nav_buttons.items():
            self.assertEqual(button.isEnabled(), key == "about")

    def test_logged_in_public_beta_user_unlocks_all_feature_pages(self) -> None:
        session = AuthSession(
            "token",
            Account(user_id="1", username="beta-user"),
            {"tradeup": True},
        )
        self.window._apply_access_session(session)

        self.assertTrue(self.window._access_allowed)
        self.assertTrue(
            all(button.isEnabled() for button in self.window.nav_buttons.values())
        )
        self.activate.assert_called_with("alchemy")

    def test_free_user_is_locked_again_when_public_beta_is_closed(self) -> None:
        enabled = AuthSession(
            "token",
            Account(user_id="1", username="free-user"),
            {"tradeup": True},
        )
        disabled = AuthSession(
            "token",
            Account(user_id="1", username="free-user"),
            {"tradeup": False},
        )
        self.window._apply_access_session(enabled)
        self.window._apply_access_session(disabled)

        self.assertFalse(self.window._access_allowed)
        for key, button in self.window.nav_buttons.items():
            self.assertEqual(button.isEnabled(), key == "about")

    def test_network_error_fails_closed_after_lease_refresh(self) -> None:
        session = AuthSession(
            "token",
            Account(
                user_id="2",
                username="member",
                member=True,
                member_until=2_000_000_000,
            ),
        )
        self.window._apply_access_session(session, error="network unavailable")

        self.assertFalse(self.window._access_allowed)
        self.assertIn(
            "暂时无法验证使用权限",
            self.window._startup_placeholder.text(),
        )


if __name__ == "__main__":
    unittest.main()
