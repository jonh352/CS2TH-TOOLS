from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.collected_json import list_collected_json, save_collected_json
from core.data_utils import QUALITY_MAP
from ui.pages.platforms import PlatformPage, format_collection_platform_counts
from ui.pages.alchemy import AlchemyPage
from ui.dialogs.wide_text_input_dialog import create_wide_text_input_dialog
from ui.dialogs.custom_alchemy_item_dialog import (
    CustomAlchemyItemDialog,
    custom_item_catalog,
)
from ui.pages.recipe_manage import _SavedRecipeRow


def _candidate(index: int = 1) -> dict:
    return {
        "float_value": 0.123456 + index / 1_000_000,
        "goods_id": str(index),
        "goods_name": "AK-47 | 夜愿（略有磨损）",
        "platform": "buff",
        "price": 128.5 + index,
    }


class MaterialCollectionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_managed_json_is_alchemy_compatible(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.collected_json.COLLECTED_JSON_DIR",
            Path(temp_dir),
        ):
            path = save_collected_json([_candidate()], "测试采集")
            entries = list_collected_json()

        self.assertEqual(path.name, "测试采集.json")
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            set(entries[0][1][0]).issuperset(
                {"float_value", "goods_id", "goods_name", "platform", "price"}
            ),
            True,
        )

    def test_collection_waits_for_explicit_replace_import(self) -> None:
        page = PlatformPage()
        emitted: list[tuple[list[dict], str]] = []
        page.import_to_alchemy_requested.connect(
            lambda items, mode: emitted.append((items, mode))
        )
        page._collection_running = True
        page._collection_started_at = 100.0
        page._pending_alchemy_import = [_candidate(1), _candidate(2)]

        with patch("ui.pages.platforms.time.monotonic", return_value=102.75):
            page._finish_collection()

        self.assertEqual(emitted, [])
        self.assertEqual(len(page._collected_items), 2)
        self.assertIn("BUFF 2 条｜悠悠 0 条｜C5 0 条｜ECO 0 条，共 2 条", page.collection_status.text())
        self.assertIn("共计 2.8 秒", page.collection_status.text())
        self.assertEqual(page.collection_status.property("collectionState"), "complete")
        self.assertFalse(page.collection_import_button.isHidden())
        self.assertFalse(page.collection_save_json_button.isHidden())

        page._import_collected_items_to_alchemy()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][1], "replace")
        page.mark_collection_imported()
        self.assertIn("BUFF 2 条", page.collection_status.text())
        self.assertIn("已导入炼金计算", page.collection_status.text())

    def test_collection_count_summary_keeps_all_platforms_visible(self) -> None:
        rows = (
            [{"platform": "buff"}] * 2
            + [{"platform": "c5"}] * 26
            + [{"platform": "eco"}]
        )
        self.assertEqual(
            format_collection_platform_counts(rows),
            "BUFF 2 条｜悠悠 0 条｜C5 26 条｜ECO 1 条，共 29 条",
        )

    def test_special_collection_uses_same_completion_actions(self) -> None:
        page = PlatformPage()
        page._collection_started_at = 20.0
        with patch("ui.pages.platforms.time.monotonic", return_value=23.2):
            page._special_collection_completed([_candidate()], [], "")
        self.assertIn("BUFF 1 条｜悠悠 0 条｜C5 0 条｜ECO 0 条，共 1 条", page.collection_status.text())
        self.assertIn("共计 3.2 秒", page.collection_status.text())
        self.assertEqual(page.collection_status.property("collectionState"), "complete")
        self.assertFalse(page.collection_import_button.isHidden())
        self.assertFalse(page.collection_save_json_button.isHidden())

    def test_shared_text_prompt_is_wide_and_uses_chinese_actions(self) -> None:
        dialog = create_wide_text_input_dialog(
            None,
            title="导入 CS2TH 配方",
            label="粘贴配方链接：",
            value="https://cs2th.cn/recipe/test",
        )
        self.assertGreaterEqual(dialog.minimumWidth(), 520)
        self.assertGreaterEqual(dialog.minimumHeight(), 180)
        self.assertEqual(dialog.textValue(), "https://cs2th.cn/recipe/test")

    def test_saved_recipe_row_has_card_spacing(self) -> None:
        row = _SavedRecipeRow(
            Path("test.json"),
            {"title": "测试配方", "recipe": {}, "saved_at": "", "mode": "scan"},
            False,
        )
        margins = row.layout().contentsMargins()
        self.assertEqual(row.objectName(), "recipeManageRow")
        self.assertGreaterEqual(margins.left(), 12)
        self.assertGreaterEqual(margins.top(), 10)

    def test_custom_item_dialog_uses_local_collection_catalog(self) -> None:
        catalog = custom_item_catalog()
        self.assertGreater(len(catalog), 0)
        dialog = CustomAlchemyItemDialog()
        self.assertTrue(dialog.box_combo.isEditable())
        self.assertTrue(dialog.skin_combo.isEditable())
        self.assertLessEqual(dialog.box_combo.maxVisibleItems(), 11)
        self.assertLessEqual(dialog.box_combo.view().maximumHeight(), 300)
        selection = dialog.selection()
        self.assertIsNotNone(selection)
        assert selection is not None
        template, box_id, low, high, quantity, price = selection
        self.assertGreater(box_id, 0)
        self.assertLessEqual(template.min_float, low)
        self.assertLess(high, template.max_float + 1e-9)
        self.assertEqual(quantity, 10)
        self.assertEqual(price, 0.0)

    def test_custom_item_action_adds_requested_quantity(self) -> None:
        template = next(iter(next(iter(custom_item_catalog().values()))))

        class FakeDialog:
            def __init__(self, _parent=None) -> None:
                pass

            def exec(self):
                from PySide6.QtWidgets import QDialog

                return QDialog.DialogCode.Accepted

            def selection(self):
                return (
                    template,
                    template.weapon_box_id[0],
                    template.min_float,
                    template.max_float,
                    10,
                    12.5,
                )

        page = AlchemyPage()
        with patch("ui.pages.alchemy.CustomAlchemyItemDialog", FakeDialog):
            page._on_add_custom_items()
        self.assertEqual(len(page._all_data), 10)
        self.assertTrue(all(row["platform"] == "custom" for row in page._all_data.values()))
        self.assertTrue(all(row["price"] == 12.5 for row in page._all_data.values()))

    def test_custom_item_action_uses_selected_collection_price(self) -> None:
        template = next(iter(next(iter(custom_item_catalog().values()))))
        box_id = int(template.weapon_box_id[0])
        quality = QUALITY_MAP.get(template.quality, template.quality)
        price_map = {
            "stat_trak" if template.stat_trak else "ordinary": {
                box_id: {quality: {1.0: {str(template.paint_index): 23.75}}}
            }
        }

        class FakeDialog:
            def __init__(self, _parent=None) -> None:
                pass

            def exec(self):
                from PySide6.QtWidgets import QDialog

                return QDialog.DialogCode.Accepted

            def selection(self):
                return (
                    template,
                    box_id,
                    template.min_float,
                    template.max_float,
                    2,
                    0.0,
                )

        page = AlchemyPage()
        with patch("ui.pages.alchemy.CustomAlchemyItemDialog", FakeDialog), patch(
            "ui.pages.alchemy.try_build_product_price_map_from_disk",
            return_value=price_map,
        ):
            page._on_add_custom_items()
        self.assertEqual(len(page._all_data), 2)
        self.assertTrue(all(row["price"] == 23.75 for row in page._all_data.values()))
        self.assertTrue(all(row["weapon_box_id"] == box_id for row in page._all_data.values()))


if __name__ == "__main__":
    unittest.main()
