import copy
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from core.purchase_batches import create_purchase_batch, add_recipe_to_purchase_batch, load_purchase_batch
from tests.test_purchase_batches import _special_recipe
from ui.pages.purchase_manage import PurchaseManagePage
from ui.widgets.purchase_batch_card import PurchaseBatchCard, purchase_recipe_matches_item_search


class PurchaseItemSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_search_matches_original_or_replacement_ignoring_case_and_spaces(self):
        entry = {"recipe": {"substrates_display": [{"name": "AK-47 | Test"}]},
                 "materials": [{"replacement": {"name": "M4A4 | 替代饰品"}}]}
        self.assertTrue(purchase_recipe_matches_item_search(entry, " ak-47 | test "))
        self.assertTrue(purchase_recipe_matches_item_search(entry, "替代"))
        self.assertFalse(purchase_recipe_matches_item_search(entry, "AWP"))
        self.assertTrue(purchase_recipe_matches_item_search(entry, " "))

    def test_search_pending_and_received_recipes_without_changing_batch(self):
        with TemporaryDirectory() as temp, patch("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp)):
            path = create_purchase_batch("搜索批次", profile_id="test", steam_id="", account_name="Test", inventory_items=[])
            add_recipe_to_purchase_batch(path, _special_recipe(), title="匹配配方")
            add_recipe_to_purchase_batch(path, _special_recipe(), title="其他配方")
            payload = load_purchase_batch(path)
            payload["recipes"][0]["materials"][0]["replacement"] = {"name": "搜索专用饰品", "min_wear": 0.1, "max_wear": 0.2}
            with patch("ui.pages.purchase_manage.list_purchase_batches", return_value=[(path, payload)]):
                page = PurchaseManagePage()
                for key in ("purchasing", "purchase_completed"):
                    for entry in payload["recipes"]:
                        for row in entry["materials"]:
                            row["status"] = "received" if key == "purchase_completed" else "pending"
                    original = copy.deepcopy(payload)
                    page._show_section("purchasing")
                    page._item_search_edit.setText("搜索专用")
                    page._item_search_timer.stop()
                    page._rebuild_purchase_batch_rows()
                    cards = [page._purchase_batch_layout.itemAt(i).widget()
                             for i in range(page._purchase_batch_layout.count())]
                    cards = [card for card in cards if isinstance(card, PurchaseBatchCard)]
                    self.assertEqual(len(cards), 1)
                    groups = cards[0]._recipe_groups()
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(len(groups[0][2]), len(payload["recipes"][0]["materials"]))
                    self.assertTrue(cards[0]._expanded)
                    page._item_search_edit.setText("不存在的饰品")
                    page._item_search_timer.stop()
                    page._rebuild_purchase_batch_rows()
                    self.assertEqual(page._purchase_batch_layout.count(), 1)
                    page._item_search_edit.clear()
                    page._item_search_timer.stop()
                    page._rebuild_purchase_batch_rows()
                    widget = page._purchase_batch_layout.itemAt(0).widget()
                    restored = [widget]
                    self.assertEqual(sum(len(card._recipe_groups()) for card in restored), 2)
                    self.assertEqual(payload, original)
                page.close()
                page.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
