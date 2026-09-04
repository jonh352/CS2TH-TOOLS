from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow, PAGE_DEFINITIONS


class NavigationHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.auth_patch = patch.object(MainWindow, "_start_auth_validation")
        self.auth_patch.start()
        self.window = MainWindow()
        self.app.processEvents()  # 执行启动时默认进入 Steam 库存的 singleShot。

    def tearDown(self) -> None:
        self.window.close()
        self.auth_patch.stop()

    def _reset_history_to_current(self) -> None:
        self.window._navigation_history.clear()
        self.window._navigation_history_index = -1
        self.window._record_current_navigation_route()

    def test_default_page_is_steam_inventory(self) -> None:
        self.assertEqual(self.window._active_page_key, "inventory")
        self.assertTrue(self.window.nav_buttons["inventory"].property("active"))

    def test_history_buttons_float_at_content_upper_left(self) -> None:
        self.window.show()
        self.app.processEvents()

        overlay = self.window._navigation_history_overlay
        self.assertEqual((overlay.x(), overlay.y()), (12, self.window.stack.y()))
        self.assertEqual(
            (
                self.window.navigation_back_button.width(),
                self.window.navigation_back_button.height(),
            ),
            (28, 20),
        )
        self.assertEqual(
            (
                self.window.navigation_forward_button.width(),
                self.window.navigation_forward_button.height(),
            ),
            (28, 20),
        )

    def test_purchase_management_is_immediately_right_of_recipe_management(self) -> None:
        definitions = list(PAGE_DEFINITIONS)
        recipe_index = definitions.index(("recipes", "配方管理"))

        self.assertEqual(self.window.size().width(), 1208)
        self.assertEqual(definitions[recipe_index + 1], ("purchases", "采购管理"))
        self.window._activate("purchases")
        self.assertEqual(self.window._active_page_key, "purchases")
        self.assertTrue(self.window.nav_buttons["purchases"].property("active"))

    def test_back_and_forward_restore_alchemy_steps(self) -> None:
        self.window._activate("alchemy")
        self._reset_history_to_current()
        alchemy = self.window.pages["alchemy"]

        alchemy.step_stack.setCurrentIndex(1)
        alchemy.step_stack.setCurrentIndex(2)
        self.assertIn("产物磨损", self.window.navigation_back_button.toolTip())

        self.window._navigate_back()
        self.assertEqual(alchemy.step_stack.currentIndex(), 1)
        self.window._navigate_back()
        self.assertEqual(alchemy.step_stack.currentIndex(), 0)
        self.window._navigate_forward()
        self.assertEqual(alchemy.step_stack.currentIndex(), 1)

    def test_recipe_and_collection_subpages_are_restored(self) -> None:
        self.window._activate("recipes")
        self._reset_history_to_current()
        recipes = self.window.pages["recipes"]
        recipes._switch_saved_view("json")

        self.window._navigate_back()
        self.assertEqual(recipes.navigation_subroute(), "recipes")
        self.window._navigate_forward()
        self.assertEqual(recipes.navigation_subroute(), "json")

        self.window._activate("platforms")
        self._reset_history_to_current()
        platforms = self.window.pages["platforms"]
        platforms._set_mode(1)
        self.window._navigate_back()
        self.assertEqual(platforms.navigation_subroute(), "recipe")
        self.window._navigate_forward()
        self.assertEqual(platforms.navigation_subroute(), "custom")

    def test_cross_page_back_restores_the_exact_subpage(self) -> None:
        self.window._activate("recipes")
        self._reset_history_to_current()
        recipes = self.window.pages["recipes"]
        recipes._switch_saved_view("json")
        self.window._activate("purchases")
        self.window._activate("alchemy")
        alchemy = self.window.pages["alchemy"]
        alchemy.step_stack.setCurrentIndex(2)

        self.window._navigate_back()
        self.assertEqual(self.window._active_page_key, "alchemy")
        self.assertEqual(alchemy.step_stack.currentIndex(), 0)
        self.window._navigate_back()
        self.assertEqual(self.window._active_page_key, "purchases")
        self.window._navigate_back()
        self.assertEqual(self.window._active_page_key, "recipes")
        self.assertEqual(recipes.navigation_subroute(), "json")

    def test_new_navigation_after_back_discards_forward_branch(self) -> None:
        self.window._activate("alchemy")
        self._reset_history_to_current()
        self.window._activate("recipes")
        self.window._activate("simulation")

        self.window._navigate_back()
        self.assertTrue(self.window.navigation_forward_button.isEnabled())
        self.window._activate("platforms")

        self.assertFalse(self.window.navigation_forward_button.isEnabled())
        self.assertEqual(
            self.window._navigation_history[-1].page_key,
            "platforms",
        )


if __name__ == "__main__":
    unittest.main()
