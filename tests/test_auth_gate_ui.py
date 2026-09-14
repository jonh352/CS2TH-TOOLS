from __future__ import annotations

import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from core.auth_client import Account, AuthSession
from ui.main_window import MainWindow


class MainWindowAccessGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.start_patch = patch.object(MainWindow, "_start_auth_validation")
        self.activate_patch = patch.object(MainWindow, "_activate")
        self.start_patch.start()
        self.activate = self.activate_patch.start()
        with patch("ui.main_window.AuthClient.load_local_session", return_value=None):
            self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.activate_patch.stop()
        self.start_patch.stop()

    def test_guest_can_browse_all_nav_but_features_stay_locked(self) -> None:
        self.assertFalse(self.window._access_allowed)
        self.assertTrue(
            all(button.isEnabled() for button in self.window.nav_buttons.values())
        )
        self.assertFalse(self.window._access_banner.isHidden())
        self.assertIn("登录并具备汰换会员权益", self.window._access_banner_label.text())

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
        self.assertTrue(self.window._access_banner.isHidden())
        self.activate.assert_called_with("alchemy")

    def test_free_user_keeps_browse_access_when_public_beta_is_closed(self) -> None:
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
        self.assertTrue(
            all(button.isEnabled() for button in self.window.nav_buttons.values())
        )
        self.assertFalse(self.window._access_banner.isHidden())
        self.assertIn("公测已关闭", self.window._access_banner_label.text())

    def test_network_error_fails_closed_but_pages_remain_browsable(self) -> None:
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
        self.assertFalse(self.window._access_banner.isHidden())
        self.assertIn(
            "暂时无法验证使用权限",
            self.window._access_banner_label.text(),
        )
        self.assertTrue(
            all(button.isEnabled() for button in self.window.nav_buttons.values())
        )

    def test_locked_page_disables_interactive_controls(self) -> None:
        self.activate_patch.stop()
        try:
            self.window._activate("about")
            about = self.window.pages["about"]
            # About stays interactive for reading / links policy.
            self.window._activate("alchemy")
            alchemy = self.window.pages["alchemy"]
            buttons = alchemy.findChildren(QPushButton)
            self.assertTrue(buttons)
            self.assertTrue(all(not btn.isEnabled() for btn in buttons))
        finally:
            self.activate = self.activate_patch.start()

    def test_account_button_shows_username_and_entitlement_tooltip(self) -> None:
        until = time.time() + 30 * 86400
        session = AuthSession(
            "token",
            Account(
                user_id="3",
                username="multi-member",
                member=True,
                member_until=until,
                subscriptions={"tradeup": until, "terminal": until + 86400},
                effective_entitlements=("tradeup", "terminal"),
            ),
        )
        self.window._apply_access_session(session)

        self.assertEqual(self.window.account_button.text(), "multi-member")
        self.assertIn("汰换会员 到期", self.window.account_button.toolTip())
        self.assertIn("终端会员 到期", self.window.account_button.toolTip())

    def test_locked_warning_preserves_validation_error(self) -> None:
        session = AuthSession("token", Account(user_id="1", username="member", member=True))
        self.window._apply_access_session(session, error="network unavailable")
        with patch.object(self.window.toast, "show_toast") as toast:
            self.window._warn_feature_locked()
        self.assertIn("暂时无法验证使用权限", toast.call_args.args[0])
        self.assertNotIn("无汰换会员权益", toast.call_args.args[0])

    def test_external_import_waits_for_validation_then_loads_once(self) -> None:
        session = AuthSession("token", Account(user_id="1", username="member", member=True))
        self.window.auth_session = session
        reference = "https://cs2th.cn/recipe/example"
        collection = Mock()
        with patch("ui.main_window.recipe_reference_from_command", return_value=reference), \
                patch.object(self.window, "_ensure_page", return_value=collection), \
                patch.object(self.window.toast, "show_toast") as toast:
            self.window.handle_external_command("external-command")
            self.window._start_auth_validation.assert_called_once()
            collection.load_recipe_reference.assert_not_called()
            self.assertEqual(self.window._pending_recipe_reference, reference)
            self.assertIn("正在验证", toast.call_args.args[0])
            self.window._auth_validation_finished(session, "")
            self.window._auth_validation_finished(session, "")
        collection.load_recipe_reference.assert_called_once_with(reference)
        self.assertEqual(self.window._pending_recipe_reference, "")


if __name__ == "__main__":
    unittest.main()
