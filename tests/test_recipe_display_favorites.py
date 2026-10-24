import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from core.alchemy_calc import select_recipe_display_results
from core.data_utils import QUALITY_MAP
from core import saved_recipes
from ui.pages.recipe_manage import RecipeManagePage
from ui.pages.alchemy import AlchemyPage


class RecipeDisplayTests(unittest.TestCase):
    def test_default_limit_and_short_results(self):
        recipes = [{"rate": i / 100} for i in range(30)]
        self.assertEqual(len(select_recipe_display_results(recipes)), 20)
        self.assertEqual(len(select_recipe_display_results(recipes[:3])), 3)
        self.assertEqual(len(select_recipe_display_results(recipes, 25)), 25)

    def test_quality_precedes_selected_metric_and_limit(self):
        low, high = list(QUALITY_MAP)[0], list(QUALITY_MAP)[-1]
        recipes = [
            {"substrate_quality": low, "rate": 10, "break_even_rate": 1},
            {"substrate_quality": high, "rate": .4, "break_even_rate": .2},
            {"substrate_quality": high, "rate": .1, "break_even_rate": .9},
        ]
        self.assertEqual(select_recipe_display_results(recipes, 1), [recipes[1]])
        self.assertEqual(select_recipe_display_results(recipes, 1, "break_even_rate"), [recipes[2]])


class RecipeFavoriteUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_favorite_persists_and_is_removed_from_favorites_view(self):
        with TemporaryDirectory() as temp, patch.object(saved_recipes, "RECIPES_DIR", Path(temp)):
            saved_recipes.invalidate_saved_recipes_cache()
            folder = saved_recipes.create_recipe_folder("测试目录")
            path = saved_recipes.save_recipe_file(
                {"cost": 1, "substrates_display": [], "products_display": []},
                title="收藏测试", mode="scan", folder_id=folder, rank=1, norm_min=0, norm_max=1,
            )
            page = RecipeManagePage()
            page.refresh_from_disk()
            page._switch_saved_view("favorites")
            self.assertEqual(len(page._rows), 0)
            saved_recipes.set_saved_recipe_favorite(path, True)
            page._rebuild_recipe_rows()
            self.assertEqual(len(page._rows), 1)
            self.assertEqual(page._rows[0]._favorite_btn.text(), "★")
            self.assertEqual(saved_recipes.list_saved_recipes()[0][1]["folder_id"], folder)
            page._rows[0]._favorite_btn.click()
            self.app.processEvents()
            self.assertEqual(len(page._rows), 0)
            saved_recipes.invalidate_saved_recipes_cache()
            self.assertFalse(saved_recipes.list_saved_recipes()[0][1]["favorite"])
            page.close()
            page.deleteLater()
            self.app.processEvents()
        saved_recipes.invalidate_saved_recipes_cache()

    def test_alchemy_result_options_restore(self):
        with patch("ui.pages.alchemy.load_alchemy_step2_wear_ui", return_value={
            "result_display_limit": 35, "result_sort_mode": "break_even_rate",
        }):
            page = AlchemyPage()
            self.assertEqual(page.step3_result_limit_spin.value(), 35)
            self.assertEqual(page.step3_result_sort_combo.currentData(), "break_even_rate")
            page.close()
            page.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
