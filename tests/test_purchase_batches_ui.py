from __future__ import annotations

import os
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from core.purchase_batches import (
    create_purchase_batch,
    list_purchase_batches,
    load_purchase_batch,
    purchase_batch_replacement_options,
)
from tests.test_purchase_batches import _special_recipe
from ui.dialogs.purchase_replacement_dialog import PurchaseReplacementDialog
from ui.pages.platforms import PlatformPage
from ui.pages.alchemy import AlchemyPage
from ui.pages.recipe_manage import RecipeManagePage
from ui.widgets.recipe_result_group import RecipeResultGroup
from ui.widgets.purchase_batch_card import PurchaseBatchCard
from ui.theme import build_stylesheet


class PurchaseBatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_recipe_manage_has_purchase_batch_tab_between_saved_views(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            page = RecipeManagePage()
            page._switch_saved_view("purchase_batches")

        self.assertEqual(page._body_stack.count(), 3)
        self.assertEqual(page._body_stack.currentIndex(), 1)
        self.assertTrue(page._purchase_batches_view_btn.isChecked())
        self.assertEqual(page._purchase_batches_view_btn.text(), "采购管理")
        self.assertEqual(page.navigation_route_label(), "配方管理 · 采购管理")

    def test_alchemy_result_group_emits_isolated_recipe_for_purchase_batch(self) -> None:
        recipe = _special_recipe()
        group = RecipeResultGroup(3, recipe, enable_save=True)
        emitted: list[tuple[int, dict]] = []
        group.add_to_purchase_batch_requested.connect(
            lambda rank, payload: emitted.append((rank, payload))
        )

        group.add_to_purchase_batch_btn.click()
        emitted[0][1]["substrates_display"].clear()

        self.assertEqual(emitted[0][0], 3)
        self.assertEqual(len(recipe["substrates_display"]), 10)

    def test_alchemy_page_has_no_bulk_purchase_batch_action(self) -> None:
        page = AlchemyPage()
        button_texts = [button.text() for button in page.findChildren(QPushButton)]

        self.assertNotIn("当前结果加入采购批次", button_texts)

    def test_alchemy_result_can_join_batch_and_duplicate_click_is_blocked(self) -> None:
        recipe = _special_recipe()
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "炼金采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            page = AlchemyPage()
            page._step3_batch_source_id = "calculation-test"
            with (
                patch.object(page, "_choose_purchase_batch_path", return_value=path),
                patch("ui.pages.alchemy.show_toast") as toast,
            ):
                page._on_recipe_add_to_purchase_batch_requested(1, recipe)
                page._on_recipe_add_to_purchase_batch_requested(1, recipe)
            batch = load_purchase_batch(path)

        self.assertEqual(len(batch["recipes"]), 1)
        self.assertEqual(len(batch["recipes"][0]["materials"]), 10)
        self.assertIn("炼金计算方案 01", batch["recipes"][0]["title"])
        self.assertTrue(
            any("已在此采购批次" in str(call.args[1]) for call in toast.call_args_list)
        )

    def test_alchemy_result_can_create_batch_and_join_it_immediately(self) -> None:
        recipe = _special_recipe()
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            page = AlchemyPage()
            page._step3_batch_source_id = "calculation-new-batch"
            with (
                patch("ui.pages.alchemy.ask_confirmation", return_value=True),
                patch(
                    "ui.pages.alchemy.get_wide_text_input",
                    return_value=("炼金采购批次", True),
                ),
                patch(
                    "ui.pages.alchemy.QInputDialog.getItem",
                    side_effect=[
                        ("＋ 新建采购批次", True),
                        ("six", True),
                    ],
                ),
                patch(
                    "ui.pages.alchemy.list_profile_entries",
                    return_value=[{"id": "six-profile", "display_name": "six"}],
                ),
                patch(
                    "ui.pages.alchemy.get_active_profile_id",
                    return_value="six-profile",
                ),
                patch(
                    "ui.pages.alchemy.load_steam_account_config_dict",
                    return_value={"steam_id": "76561198000000006"},
                ),
                patch(
                    "ui.pages.alchemy.load_profile_inventory_items",
                    return_value=[{"assetid": "old-asset"}],
                ),
                patch("ui.pages.alchemy.show_toast"),
            ):
                page._on_recipe_add_to_purchase_batch_requested(1, recipe)
            entries = list_purchase_batches()

        self.assertEqual(len(entries), 1)
        batch = entries[0][1]
        self.assertEqual(batch["name"], "炼金采购批次")
        self.assertEqual(batch["profile_id"], "six-profile")
        self.assertEqual(batch["baseline_asset_ids"], ["old-asset"])
        self.assertEqual(len(batch["recipes"]), 1)
        self.assertIn("炼金计算方案 01", batch["recipes"][0]["title"])

    def test_create_batch_binds_selected_account_and_inventory_baseline(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            page = RecipeManagePage()
            with (
                patch("ui.pages.recipe_manage.ask_confirmation", return_value=True),
                patch(
                    "ui.pages.recipe_manage.get_wide_text_input",
                    return_value=("8月采购", True),
                ),
                patch(
                    "ui.pages.recipe_manage.QInputDialog.getItem",
                    return_value=("six", True),
                ),
                patch(
                    "ui.pages.recipe_manage.list_profile_entries",
                    return_value=[{"id": "six-profile", "display_name": "six"}],
                ),
                patch(
                    "ui.pages.recipe_manage.get_active_profile_id",
                    return_value="six-profile",
                ),
                patch(
                    "ui.pages.recipe_manage.load_steam_account_config_dict",
                    return_value={"steam_id": "76561198000000006"},
                ),
                patch(
                    "ui.pages.recipe_manage.load_profile_inventory_items",
                    return_value=[{"assetid": "old-asset"}],
                ),
                patch("ui.pages.recipe_manage.show_toast"),
            ):
                page._create_purchase_batch()
            entries = list_purchase_batches()

        self.assertEqual(len(entries), 1)
        batch = entries[0][1]
        self.assertEqual(batch["name"], "8月采购")
        self.assertEqual(batch["profile_id"], "six-profile")
        self.assertEqual(batch["baseline_asset_ids"], ["old-asset"])

    def test_special_solution_adds_directly_to_selected_batch(self) -> None:
        recipe = _special_recipe()
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "8月采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            page = PlatformPage()
            page._special_payload = {"target_name": "特殊目标"}
            label = "8月采购 · six · 0配方/0件"
            with (
                patch(
                    "ui.pages.platforms.QInputDialog.getItem",
                    return_value=(label, True),
                ),
                patch("ui.pages.platforms.show_toast"),
            ):
                page._add_special_solution_to_purchase_batch(recipe)
            batch = load_purchase_batch(path)

        self.assertEqual(len(batch["recipes"]), 1)
        self.assertEqual(len(batch["recipes"][0]["materials"]), 10)

    def test_all_special_solutions_can_be_added_with_one_batch_choice(self) -> None:
        recipes = [_special_recipe(), _special_recipe()]
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "8月采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            page = PlatformPage()
            page._special_payload = {"target_name": "特殊目标"}
            page._special_solution_recipes = recipes
            with (
                patch.object(page, "_choose_purchase_batch_path", return_value=path),
                patch("ui.pages.platforms.show_toast"),
            ):
                page._add_all_special_solutions_to_purchase_batch()
            batch = load_purchase_batch(path)

        self.assertEqual(len(batch["recipes"]), 2)

    def test_batch_card_filters_missing_materials(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            add_recipe_to_purchase_batch(path, _special_recipe())
            batch = load_purchase_batch(path)
            card = PurchaseBatchCard(path, batch)
            card._filter.setCurrentText("待购买")

        self.assertTrue(card._expanded)
        self.assertEqual(card._toggle_button.text(), "收起材料")
        self.assertEqual(card._table.rowCount(), 10)

    def test_batch_action_row_is_left_aligned(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            card = PurchaseBatchCard(path, load_purchase_batch(path))
            card.resize(1200, 240)
            card.show()
            self._app.processEvents()

        self.assertLess(card._filter.x(), card.width() // 2)

    def test_reconcile_inventory_button_matches_view_button_on_its_left(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            card = PurchaseBatchCard(path, load_purchase_batch(path))
            card.resize(1200, 240)
            card.show()
            self._app.processEvents()

        self.assertEqual(
            card._reconcile_button.objectName(),
            "purchaseBatchReconcileBtn",
        )
        self.assertEqual(
            card._reconcile_button.size(),
            card._toggle_button.size(),
        )
        self.assertLess(
            card._reconcile_button.x(),
            card._toggle_button.x(),
        )
        self.assertEqual(
            card._reconcile_button.x() + card._reconcile_button.width() + 12,
            card._toggle_button.x(),
        )
        stylesheet = build_stylesheet("light")
        self.assertIn("QPushButton#purchaseBatchReconcileBtn", stylesheet)
        self.assertIn("background: #16A34A", stylesheet)

    def test_file_picker_starts_in_saved_json_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            saved_json_dir = Path(temp_dir) / "collected_json"
            page = AlchemyPage()
            with (
                patch("ui.pages.alchemy.COLLECTED_JSON_DIR", saved_json_dir),
                patch(
                    "ui.pages.alchemy.QFileDialog.getOpenFileNames",
                    return_value=([], ""),
                ) as choose_files,
            ):
                page._on_select_file()

            self.assertTrue(saved_json_dir.is_dir())
            self.assertEqual(choose_files.call_args.args[2], str(saved_json_dir))

    def test_batch_table_groups_recipes_by_added_time_and_buttons_fit_rows(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            add_recipe_to_purchase_batch(path, _special_recipe(), title="较早加入的配方")
            add_recipe_to_purchase_batch(path, _special_recipe(), title="较晚加入的配方")
            batch = load_purchase_batch(path)
            batch["recipes"].reverse()  # 显示顺序应依 added_at，而不是文件内列表顺序。
            card = PurchaseBatchCard(path, batch, expanded=True)

        self.assertEqual(len(card._tables), 2)
        self.assertEqual([table.rowCount() for table in card._tables], [10, 10])
        self.assertEqual(
            [button.text() for button in card._recipe_group_buttons],
            ["①", "②"],
        )
        self.assertEqual(
            card._recipe_group_buttons[0].toolTip(),
            "较早加入的配方",
        )
        first_entry_id = str(batch["recipes"][1]["id"])
        card._recipe_group_buttons[0].click()
        self.assertIn(first_entry_id, card._expanded_recipe_ids)
        self.assertGreaterEqual(card._table.rowHeight(0), 48)
        purchase_button = card._table.cellWidget(0, 6).findChild(QPushButton)
        action_buttons = card._table.cellWidget(0, 7).findChildren(QPushButton)
        self.assertGreaterEqual(purchase_button.minimumHeight(), 34)
        self.assertTrue(action_buttons)
        self.assertTrue(all(button.minimumHeight() >= 34 for button in action_buttons))

    def test_single_purchase_batch_starts_collapsed(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "炼金采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            add_recipe_to_purchase_batch(path, _special_recipe())
            page = RecipeManagePage()
            page._switch_saved_view("purchase_batches")
            cards = [
                page._purchase_batch_layout.itemAt(index).widget()
                for index in range(page._purchase_batch_layout.count())
                if isinstance(
                    page._purchase_batch_layout.itemAt(index).widget(),
                    PurchaseBatchCard,
                )
            ]

        self.assertEqual(len(cards), 1)
        self.assertFalse(cards[0]._expanded)
        self.assertIsNone(cards[0]._table)
        self.assertEqual(cards[0]._toggle_button.text(), "查看材料（10）")

    def test_purchase_batch_view_state_survives_subpage_switch_and_refresh(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "炼金采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            add_recipe_to_purchase_batch(path, _special_recipe())
            page = RecipeManagePage()
            page._switch_saved_view("purchase_batches")
            first_card = next(
                page._purchase_batch_layout.itemAt(index).widget()
                for index in range(page._purchase_batch_layout.count())
                if isinstance(
                    page._purchase_batch_layout.itemAt(index).widget(),
                    PurchaseBatchCard,
                )
            )
            first_card._filter.setCurrentText("待购买")
            self.assertTrue(first_card._expanded)
            expanded_recipe_ids = set(first_card._expanded_recipe_ids)

            page._switch_saved_view("json")
            page._switch_saved_view("purchase_batches")
            page.refresh_from_disk()
            restored_card = next(
                page._purchase_batch_layout.itemAt(index).widget()
                for index in range(page._purchase_batch_layout.count())
                if isinstance(
                    page._purchase_batch_layout.itemAt(index).widget(),
                    PurchaseBatchCard,
                )
            )

        self.assertTrue(restored_card._expanded)
        self.assertEqual(restored_card._filter.currentText(), "待购买")
        self.assertEqual(restored_card._expanded_recipe_ids, expanded_recipe_ids)
        self.assertEqual(restored_card._toggle_button.text(), "收起材料")

    def test_view_material_button_is_large_right_and_vertically_centered(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            card = PurchaseBatchCard(path, load_purchase_batch(path))
            card.resize(1200, 260)
            card.show()
            self._app.processEvents()

        button_center = card._toggle_button.mapTo(
            card,
            card._toggle_button.rect().center(),
        )
        content_top = card._title_label.y()
        content_bottom = card._filter.y() + card._filter.height()
        self.assertLess(
            abs(button_center.y() - ((content_top + content_bottom) // 2)),
            4,
        )
        self.assertLessEqual(
            abs(
                (card._toggle_button.x() + card._toggle_button.width())
                - (card.width() - 14)
            ),
            1,
        )
        self.assertGreaterEqual(card._toggle_button.width(), 190)
        self.assertGreaterEqual(card._toggle_button.height(), 42)

    def test_purchase_batch_account_can_be_changed(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="wrong-profile",
                steam_id="wrong-steam",
                account_name="wrong",
                inventory_items=[],
            )
            page = RecipeManagePage()
            with (
                patch(
                    "ui.pages.recipe_manage.list_profile_entries",
                    return_value=[
                        {"id": "wrong-profile", "display_name": "wrong"},
                        {"id": "six-profile", "display_name": "six"},
                    ],
                ),
                patch(
                    "ui.pages.recipe_manage.QInputDialog.getItem",
                    return_value=("six", True),
                ),
                patch("ui.pages.recipe_manage.ask_confirmation", return_value=True),
                patch(
                    "ui.pages.recipe_manage.load_steam_account_config_dict",
                    return_value={"steam_id": "six-steam"},
                ),
                patch(
                    "ui.pages.recipe_manage.load_profile_inventory_items",
                    return_value=[{"assetid": "six-existing"}],
                ),
                patch("ui.pages.recipe_manage.show_toast"),
            ):
                page._change_purchase_batch_account(path)
            batch = load_purchase_batch(path)

        self.assertEqual(batch["profile_id"], "six-profile")
        self.assertEqual(batch["steam_id"], "six-steam")
        self.assertEqual(batch["account_name"], "six")
        self.assertEqual(batch["baseline_asset_ids"], ["six-existing"])

    def test_replacement_dialog_only_enables_safe_candidates(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            batch = load_purchase_batch(path)
            row_id = batch["recipes"][0]["materials"][0]["row_id"]
            options, target_text = purchase_batch_replacement_options(
                batch, entry_id, row_id
            )
            dialog = PurchaseReplacementDialog(
                None,
                options=options,
                target_text=target_text,
            )
            dialog.table.selectRow(0)

        self.assertTrue(options[0]["safe"])
        self.assertTrue(dialog._requires_manual_wear)
        self.assertFalse(dialog.manual_wear_container.isHidden())
        self.assertFalse(dialog.adopt_button.isEnabled())
        dialog.purchase_price_edit.setText("12.50")
        first = options[0]
        prefix = int(float(first["min_wear"]) * 1_000_000) / 1_000_000
        dialog.manual_wear_edit.setText(f"{prefix:.6f}")
        self.assertTrue(dialog.adopt_button.isEnabled())

    def test_special_replacement_intersects_strict_interval_with_six_digit_prefix(self) -> None:
        option = {
            "name": "测试材料",
            "safe": True,
            "range_mode": "target_interval",
            "min_wear": 0.0,
            "max_wear": 0.123456789,
        }
        dialog = PurchaseReplacementDialog(
            None,
            options=[option],
            target_text="特殊磨损目标区间",
        )
        dialog.table.selectRow(0)
        dialog.purchase_price_edit.setText("8.88")
        dialog.manual_wear_edit.setText("0.123456")
        dialog._accept_selected()
        chosen = dialog.chosen_option()

        self.assertTrue(dialog._requires_manual_wear)
        self.assertFalse(dialog.manual_wear_container.isHidden())
        self.assertEqual(chosen["manual_wear"], 0.123456)
        self.assertGreaterEqual(chosen["min_wear"], option["min_wear"])
        self.assertLessEqual(chosen["max_wear"], option["max_wear"])
        self.assertLess(chosen["max_wear"], 0.123457)
        self.assertEqual(chosen["purchase_price"], 8.88)
        wear_text = "0.000000000000000000 ～ 0.123456789000000000"
        self.assertLessEqual(dialog.table.columnWidth(0), 210)
        self.assertGreaterEqual(
            dialog.table.columnWidth(2),
            dialog.table.fontMetrics().horizontalAdvance(wear_text) + 28,
        )
        self.assertEqual(dialog.table.rowHeight(0), 48)
        purchase_button = dialog.table.cellWidget(0, 3).findChild(QPushButton)
        self.assertIsNotNone(purchase_button)
        self.assertGreaterEqual(purchase_button.minimumWidth(), 150)
        self.assertGreaterEqual(purchase_button.minimumHeight(), 36)

    def test_normal_replacement_requires_exact_first_six_decimals(self) -> None:
        option = {
            "name": "普通替代材料",
            "safe": True,
            "range_mode": "not_higher",
            "min_wear": 0.0,
            "max_wear": 1.0,
        }
        dialog = PurchaseReplacementDialog(
            None,
            options=[option],
            target_text="普通配方",
        )
        dialog.table.selectRow(0)
        dialog.manual_wear_edit.setText("0.164959")
        dialog.purchase_price_edit.setText("6.66")
        _value, match_low, match_high = dialog._manual_match_interval(option)

        self.assertEqual(dialog._manual_decimals, 6)
        self.assertLessEqual(match_low, 0.164959001)
        self.assertGreaterEqual(match_high, 0.164959999)
        self.assertLess(match_high, 0.164960)
        self.assertTrue(dialog.adopt_button.isEnabled())

    def test_purchase_batch_displays_item_prices_and_recipe_cost(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            from core.purchase_batches import add_recipe_to_purchase_batch

            add_recipe_to_purchase_batch(path, _special_recipe(), title="配方1")
            add_recipe_to_purchase_batch(path, _special_recipe(), title="配方2")
            batch = load_purchase_batch(path)
            card = PurchaseBatchCard(path, batch)
            card._set_expanded(True)

        self.assertEqual(card._table.horizontalHeaderItem(3).text(), "采集价格")
        self.assertEqual(card._table.item(0, 3).text(), "¥10.00")
        self.assertEqual(card._table.columnWidth(5), 120)
        self.assertEqual(card._table.columnWidth(6), 96)
        summaries = card.findChildren(QLabel)
        self.assertTrue(any("成本 ¥100.00" in label.text() for label in summaries))
        self.assertIn("批次总成本 ¥200.00", card._summary_label.text())


if __name__ == "__main__":
    unittest.main()
