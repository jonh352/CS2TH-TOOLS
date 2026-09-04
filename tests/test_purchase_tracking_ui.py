from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTableWidget

from core.alchemy_quality import get_name_map
from core.data_utils import wear_as_float32
from core.purchase_tracking import start_purchase_batch
from ui.pages.inventory import InventoryPage
from ui.pages.recipe_manage import _SavedRecipeRow


def _tracked_recipe() -> dict:
    template = next(
        template
        for template in get_name_map().values()
        if template.upper_skins and template.max_float > template.min_float
    )
    name = (
        f"{template.weapon_name} | {template.skin_name}"
        if template.skin_name
        else template.weapon_name
    )
    wear = wear_as_float32(
        (float(template.min_float) + float(template.max_float)) / 2.0
    )
    return {
        "cost": 10.0,
        "expected": 11.0,
        "rate": 0.1,
        "substrates_display": [
            {
                "name": name,
                "float_value": wear,
                "price": 10.0,
                "platform": "buff",
                "purchase_link": "https://example.invalid/buy",
            }
        ],
        "products_display": [],
    }


class PurchaseTrackingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_saved_recipe_does_not_show_purchase_controls(self) -> None:
        recipe = _tracked_recipe()
        start_purchase_batch(
            recipe,
            profile_id="six-profile",
            steam_id="76561198000000006",
            account_name="six",
            inventory_items=[],
        )
        payload = {"title": "采购测试", "recipe": recipe, "mode": "scan"}
        row = _SavedRecipeRow(Path("recipe.json"), payload, False)

        forbidden = {"开始采购", "重新开始", "全部已购买", "核对库存"}
        button_texts = {button.text() for button in row.findChildren(QPushButton)}
        label_text = " ".join(label.text() for label in row.findChildren(QLabel))
        self.assertTrue(forbidden.isdisjoint(button_texts))
        self.assertNotIn("采购入库", label_text)

        row._instantiate_recipe_group()
        assert row._group is not None
        headers = {
            table.horizontalHeaderItem(column).text()
            for table in row._group.findChildren(QTableWidget)
            for column in range(table.columnCount())
            if table.horizontalHeaderItem(column) is not None
        }
        self.assertNotIn("入库状态", headers)

    def test_inventory_refresh_runs_reconciliation_and_reports_progress(self) -> None:
        page = InventoryPage()
        items = [{"assetid": "new-asset"}]
        with (
            patch("ui.pages.inventory._atomic_json_write"),
            patch.object(page, "_reload_accounts"),
            patch(
                "ui.pages.inventory.reconcile_all_purchase_records_for_profile",
                return_value={"matched": 1, "waiting": 2, "missing_review": 3},
            ) as reconcile,
        ):
            page._fetch_finished("six-profile", items, None, "", "库存更新完成")

        reconcile.assert_called_once_with("six-profile", items)
        self.assertIn("新入库 1 件", page.status_label.text())
        self.assertIn("仍待入库 2 件", page.status_label.text())
        self.assertIn("3 件已入库材料离开库存", page.status_label.text())


if __name__ == "__main__":
    unittest.main()
