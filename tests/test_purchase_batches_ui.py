from __future__ import annotations

import os
import math
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QFrame, QLabel, QLineEdit, QPushButton, QWidget

from core.alchemy_quality import get_name_map
from core.data_utils import tradeup_display_quality, wear_as_float32
from core.purchase_batches import (
    add_recipe_to_purchase_batch,
    create_purchase_batch,
    list_purchase_batches,
    load_purchase_batch,
    mark_all_purchase_batch_materials_ordered,
    purchase_batch_replacement_options,
    reconcile_purchase_batches_for_profile,
)
from tests.test_purchase_batches import _inventory, _name, _special_recipe, _template_pair
from ui.dialogs.purchase_replacement_dialog import PurchaseReplacementDialog
from ui.dialogs.alert_dialog import ConfirmDialog
from ui.dialogs.steam_tradeup_dialog import SteamTradeupDialog
from ui.dialogs.steam_tradeup_result_dialog import SteamTradeupResultDialog
from ui.pages.platforms import PlatformPage
from ui.pages.alchemy import AlchemyPage
from ui.pages.alchemy_simulation import AlchemySimulationPage
from ui.pages.purchase_manage import PurchaseManagePage
from ui.pages.recipe_manage import RecipeManagePage
from ui.widgets.purchase_qr_label import QrSlot, normalize_purchase_url_key
from ui.widgets.recipe_result_group import RecipeResultGroup
from ui.widgets.purchase_batch_card import (
    PurchaseBatchCard,
    _purchase_recipe_result_snapshot,
    _purchase_platform_label,
)
from ui.theme import build_stylesheet


def _recipe_with_purchase_links(*, viewed: bool = True, count: int = 2) -> dict:
    substrates = []
    purchase_viewed: dict[str, bool] = {}
    for index in range(count):
        url = f"https://buff.163.com/goods/{index}"
        substrates.append(
            {
                "name": f"AK-47 | Test {index}",
                "float_value": 0.1 + index * 0.01,
                "price": 10.0,
                "platform": "buff",
                "purchase_link": url,
                "weapon_box": "box",
            }
        )
        if viewed:
            purchase_viewed[normalize_purchase_url_key(url)] = True
    recipe = {
        "substrates_display": substrates,
        "products_display": [],
        "cost": 10.0 * count,
    }
    if purchase_viewed:
        recipe["purchase_viewed"] = purchase_viewed
    return recipe


class PurchaseBatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_purchase_platform_label_uses_short_names(self) -> None:
        self.assertEqual(_purchase_platform_label("buff"), "BUFF")
        self.assertEqual(_purchase_platform_label("yyyp"), "悠悠")
        self.assertEqual(_purchase_platform_label("c5"), "C5")
        self.assertEqual(_purchase_platform_label("eco"), "ECO")
        self.assertEqual(
            _purchase_platform_label("", "https://buff.163.com/goods/1"),
            "BUFF",
        )

    def test_tradeup_confirmation_supports_double_width(self) -> None:
        dialog = ConfirmDialog("确认一键汰换", "材料", box_width=800)
        box = next(
            frame
            for frame in dialog.findChildren(QFrame)
            if frame.objectName() == "loginBox"
        )

        self.assertEqual(box.width(), 800)

    def test_tradeup_risk_acknowledgement_is_required(self) -> None:
        dialog = ConfirmDialog(
            "确认一键汰换",
            "材料",
            warning_text="风险提示与免责声明",
            acknowledgement_text="我已阅读并理解上述风险",
        )
        checkbox = dialog.findChild(
            QCheckBox, "confirmationRiskAcknowledgement"
        )
        warning = dialog.findChild(QLabel, "confirmationRiskNotice")

        self.assertIsNotNone(checkbox)
        self.assertIn("免责声明", warning.text())
        self.assertFalse(dialog._ok_btn.isEnabled())
        checkbox.setChecked(True)
        self.assertTrue(dialog._ok_btn.isEnabled())

    def test_tradeup_dialog_allows_qr_or_credentials_login(self) -> None:
        with patch(
            "ui.dialogs.steam_tradeup_dialog.has_saved_tradeup_session",
            return_value=False,
        ):
            dialog = SteamTradeupDialog(
                None,
                {"profile_id": "profile", "steam_id": "76561198000000001"},
            )

        self.assertTrue(dialog._qr_mode_btn.isChecked())
        self.assertTrue(dialog._credentials_panel.isHidden())
        dialog._credentials_mode_btn.click()
        self.assertFalse(dialog._credentials_panel.isHidden())
        self.assertEqual(
            dialog._password_edit.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        dialog._account_edit.setText("steam-login")
        dialog._password_edit.setText("password")
        dialog._guard_edit.setText("ABCDE")
        with patch.object(dialog, "_start_worker") as start_worker:
            dialog._start_authorization()
        auth = start_worker.call_args.args[0]

        self.assertEqual(auth["mode"], "credentials")
        self.assertEqual(auth["account_name"], "steam-login")
        self.assertEqual(auth["guard_code"], "ABCDE")
        dialog._running = False
        dialog.close()

    def test_tradeup_success_dialog_shows_product_image_and_details(self) -> None:
        _material, product = _template_pair()
        dialog = SteamTradeupResultDialog(
            None,
            [
                {
                    "asset_id": "output-asset",
                    "paint_index": str(product.paint_index),
                    "name": _name(product),
                    "float_value": 0.123456789,
                    "price": 88.8,
                }
            ],
        )

        image = dialog.findChild(QLabel, "steamTradeupResultImage")
        self.assertIsNotNone(image)
        self.assertIsNotNone(image.pixmap())
        self.assertFalse(image.pixmap().isNull())
        self.assertIn(
            _name(product),
            dialog.findChild(QLabel, "steamTradeupResultName").text(),
        )
        meta = dialog.findChild(QLabel, "steamTradeupResultMeta").text()
        self.assertIn("0.1234567890", meta)
        self.assertIn("¥88.80", meta)

    def test_entering_ready_section_refreshes_automatically(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            page = PurchaseManagePage()
            with patch.object(page, "_refresh_ready_recipes") as refresh:
                page._show_section("ready")
                page._show_section("ready")

        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(page.navigation_subroute(), "ready")

    def test_ready_and_history_default_to_inventory_active_account(self) -> None:
        profiles = [
            {"id": "first-profile", "display_name": "first"},
            {"id": "active-profile", "display_name": "active"},
        ]
        with (
            TemporaryDirectory() as temp_dir,
            patch("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir)),
            patch(
                "ui.pages.purchase_manage.list_profile_entries",
                return_value=profiles,
            ),
            patch(
                "ui.pages.purchase_manage.get_active_profile_id",
                return_value="active-profile",
            ),
            patch("ui.pages.purchase_manage.show_toast"),
        ):
            page = PurchaseManagePage()
            page._show_section("ready")
            ready_profile = page._selected_profile_id()
            page._account_combo.setCurrentIndex(0)
            page._show_section("history")
            history_profile = page._selected_profile_id()

        self.assertEqual(ready_profile, "active-profile")
        self.assertEqual(history_profile, "active-profile")

    def test_real_inventory_recipe_enters_simulation_before_tradeup(self) -> None:
        recipe = _special_recipe()
        material = next(
            template
            for template in get_name_map().values()
            if template.upper_skins
            and template.max_float > template.min_float
            and tradeup_display_quality(template) not in {"隐秘", "非凡"}
        )
        material_name = _name(material)
        material_wear = wear_as_float32(
            (float(material.min_float) + float(material.max_float)) / 2.0
        )
        emitted = []
        for index, substrate in enumerate(recipe["substrates_display"]):
            substrate.update(
                {
                    "name": material_name,
                    "float_value": material_wear,
                    "platform": "inventory",
                    "steam_assetid": f"asset-{index}",
                    "steam_profile_id": "profile",
                    "steam_id": "76561198000000001",
                }
            )
        group = RecipeResultGroup(1, recipe, enable_save=True)
        group.simulate_tradeup_requested.connect(emitted.append)

        self.assertIsNotNone(group.simulate_tradeup_btn)
        self.assertFalse(group.simulate_tradeup_btn.isHidden())
        group.simulate_tradeup_btn.click()
        self.assertEqual(len(emitted), 1)

        plan = {
            "source": "steam_inventory_recipe",
            "title": "真实库存配方",
            "profile_id": "profile",
            "steam_id": "76561198000000001",
            "asset_ids": [f"asset-{index}" for index in range(10)],
            "materials": [
                {
                    "asset_id": f"asset-{index}",
                    "name": row["name"],
                    "float_value": row["float_value"],
                }
                for index, row in enumerate(recipe["substrates_display"])
            ],
        }
        page = AlchemySimulationPage()
        with patch.object(page, "_on_start_calculate_clicked") as calculate:
            error = page.import_verified_tradeup_plan(plan)
            self._app.processEvents()

        self.assertIsNone(error)
        calculate.assert_called_once_with()
        self.assertIsNotNone(page._verified_tradeup_plan)
        self.assertTrue(page._execute_tradeup_btn.isHidden())

        page._results_section.show()
        page.show()
        self._app.processEvents()
        self.assertFalse(page._execute_tradeup_btn.isHidden())

        page._cards[0]._wear_edit.setText("0.0000000001")
        page._on_substrate_input_changed()
        self.assertIsNone(page._verified_tradeup_plan)
        self.assertTrue(page._execute_tradeup_btn.isHidden())

    def test_one_click_tradeup_only_enables_after_all_ten_items_arrive(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "一键汰换",
                profile_id="tradeup-profile",
                steam_id="76561198000000001",
                account_name="tradeup",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe())
            card = PurchaseBatchCard(path, load_purchase_batch(path), expanded=True)
            self.assertFalse(card._recipe_one_click_buttons[0].isEnabled())

            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "tradeup-profile",
                [_inventory(f"asset-{index}") for index in range(10)],
            )
            card._reload()
            button = card._recipe_one_click_buttons[0]
            self.assertTrue(button.isEnabled())
            emitted: list[dict] = []
            card.simulate_tradeup_requested.connect(emitted.append)
            button.click()

        self.assertEqual(button.text(), "模拟并汰换")
        self.assertEqual(len(emitted), 1)
        plan = emitted[0]
        self.assertEqual(len(plan["asset_ids"]), 10)

    def test_purchase_recipe_expansion_shows_current_product_details(self) -> None:
        recipe = _special_recipe()
        material, product = _template_pair()
        recipe["products_display"] = [
            {
                "name": _name(product),
                "float_value": recipe["special_wear_output_float"],
                "weapon_box": "测试收藏品",
                "price": 168.88,
                "prob": 1.0,
            }
        ]
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "产物展示",
                profile_id="product-profile",
                steam_id="76561198000000001",
                account_name="product",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, recipe)
            card = PurchaseBatchCard(path, load_purchase_batch(path), expanded=True)

        self.assertEqual(len(card._product_tables), 1)
        table = card._product_tables[0]
        self.assertEqual(table.rowCount(), len(material.upper_skins))
        self.assertEqual(table.horizontalHeaderItem(1).text(), "磨损度")
        self.assertEqual(table.horizontalHeaderItem(3).text(), "价格")
        self.assertEqual(table.horizontalHeaderItem(4).text(), "概率")
        self.assertIn("168.88", table.item(0, 3).text())
        self.assertEqual(
            table.item(0, 4).text(),
            f"{1 / len(material.upper_skins):.2%}",
        )
        summaries = [
            label.text()
            for label in card.findChildren(QLabel)
            if label.objectName() == "purchaseBatchProductSummary"
        ]
        self.assertEqual(len(summaries), 1)
        self.assertIn(
            f"期望：{168.88 / len(material.upper_skins):.2f}",
            summaries[0],
        )

    def test_purchase_product_wear_uses_current_received_material_float(self) -> None:
        recipe = _special_recipe()
        material, product = _template_pair()
        recipe["products_display"] = [
            {
                "name": _name(product),
                "float_value": 0.0,
                "weapon_box": "测试收藏品",
                "price": 50.0,
                "prob": 1.0,
            }
        ]
        entry = {
            "recipe": recipe,
            "materials": [
                {
                    "substrate_index": index,
                    "status": "received",
                    "matched_float": float(material.max_float),
                }
                for index in range(10)
            ],
        }

        snapshot = _purchase_recipe_result_snapshot(entry)

        self.assertNotEqual(snapshot["products_display"][0]["float_value"], 0.0)
        self.assertAlmostEqual(
            snapshot["products_display"][0]["float_value"],
            float(product.max_float),
            places=6,
        )

    def test_purchase_management_is_a_standalone_page(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            page = PurchaseManagePage()

        self.assertEqual(page.navigation_route_label(), "采购管理")
        self.assertEqual(page._title_count_label.text(), "共0个批次 · 0个配方 · 0件材料")
        self.assertEqual(
            [button.text() for button in page._section_buttons.values()],
            ["采购管理", "可炼金配方", "已采购完成", "已炼金批次", "汰换记录与统计"],
        )
        page._show_section("history")
        self.assertEqual(page.navigation_subroute(), "history")
        self.assertTrue(page._start_date.isVisibleTo(page))
        self.assertTrue(page._refresh_ready_btn.isVisibleTo(page))
        self.assertEqual(page._refresh_ready_btn.text(), "刷新炼金记录")
        recipe_page = RecipeManagePage()
        self.assertEqual(recipe_page._body_stack.count(), 2)
        self.assertFalse(hasattr(recipe_page, "_purchase_batches_view_btn"))

    def test_tradeup_history_uses_numbered_image_cards_and_summary(self) -> None:
        material, product = _template_pair()
        now = datetime.now().astimezone().isoformat()
        records = [
            {
                "batch_path": "batch.json",
                "batch_name": "测试批次",
                "recipe_entry_id": "recipe-1",
                "recipe_index": 2,
                "profile_id": "profile",
                "account_name": "账号",
                "completed_at": now,
                "materials": [
                    {"name": _name(material), "price": 2.0} for _ in range(10)
                ],
                "products": [{"name": _name(product), "price": 30.0}],
                "material_cost": 20.0,
                "output_value": 30.0,
                "profit": 10.0,
            }
        ]
        with (
            TemporaryDirectory() as temp_dir,
            patch("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir)),
            patch("ui.pages.purchase_manage.list_tradeup_records", return_value=records),
        ):
            page = PurchaseManagePage()
            page._show_section("history")

        self.assertEqual(len(page.findChildren(QFrame, "tradeupHistoryDashboard")), 1)
        cards = page.findChildren(QFrame, "tradeupHistoryItemCard")
        self.assertEqual(len(cards), 10)
        self.assertEqual(len(page.findChildren(QFrame, "tradeupHistoryProductCard")), 1)
        self.assertIn(
            "测试批次　·　配方 02",
            "\n".join(label.text() for label in page.findChildren(QLabel, "tradeupHistoryRecordMeta")),
        )
        captions = [
            label.text()
            for label in page.findChildren(QLabel, "tradeupHistoryMetricCaption")
        ]
        self.assertEqual(
            captions,
            ["汰换次数", "成功率", "保本率", "汰换收益", "汰换总成本价", "汰换总价值"],
        )
        self.assertEqual(len(page.findChildren(QFrame, "tradeupHistoryDayHeader")), 1)
        self.assertIsNotNone(page.findChild(QWidget, "tradeupHistoryTrendChart"))

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

    def test_recipe_result_group_warns_before_recalc_when_all_links_viewed(self) -> None:
        recipe = _recipe_with_purchase_links(viewed=True, count=2)
        group = RecipeResultGroup(
            1,
            recipe,
            enable_save=True,
            get_substrate_action_state=lambda _slot: "neutral",
            set_substrate_action_state=lambda _slot, _state: None,
        )

        self.assertTrue(group.should_warn_before_recalc())

        recipe["purchase_viewed"].pop(next(iter(recipe["purchase_viewed"])))
        group._refresh_purchase_cells()
        self.assertFalse(group.should_warn_before_recalc())

        recipe = _recipe_with_purchase_links(viewed=True, count=2)
        group = RecipeResultGroup(
            1,
            recipe,
            enable_save=True,
            get_substrate_action_state=lambda _slot: "excluded",
            set_substrate_action_state=lambda _slot, _state: None,
        )
        self.assertTrue(group.should_warn_before_recalc())

        recipe = _recipe_with_purchase_links(viewed=True, count=2)
        group = RecipeResultGroup(
            1,
            recipe,
            enable_save=True,
            get_substrate_action_state=lambda _slot: "neutral",
            set_substrate_action_state=lambda _slot, _state: None,
        )
        group.mark_added_to_purchase_batch()
        self.assertTrue(group.should_warn_before_recalc())

        recipe = _recipe_with_purchase_links(viewed=True, count=2)
        group = RecipeResultGroup(
            1,
            recipe,
            enable_save=True,
            get_substrate_action_state=lambda _slot: "excluded",
            set_substrate_action_state=lambda _slot, _state: None,
        )
        group.mark_added_to_purchase_batch()
        self.assertFalse(group.should_warn_before_recalc())

    def test_alchemy_page_confirms_before_recalc_when_recipe_ready(self) -> None:
        page = AlchemyPage()
        page._selected_data = [{"name": "test", "float_value": 0.1}]
        recipe = _recipe_with_purchase_links(viewed=True, count=2)
        group = RecipeResultGroup(
            1,
            recipe,
            enable_save=True,
            get_substrate_action_state=lambda _slot: "neutral",
            set_substrate_action_state=lambda _slot, _state: None,
        )
        page._step3_recipe_groups = [group]

        self.assertTrue(page._step3_should_confirm_recalc())

        with patch("ui.pages.alchemy.ask_confirmation", return_value=False) as confirm:
            page._on_step3_start_calc()

        confirm.assert_called_once()
        self.assertFalse(page._step3_calc_running)

        with patch("ui.pages.alchemy.ask_confirmation", return_value=True):
            with patch("ui.pages.alchemy.FetchPriceWorker") as worker_cls:
                worker = worker_cls.return_value
                page._on_step3_start_calc()

        self.assertTrue(page._step3_calc_running)
        worker_cls.assert_called_once()
        worker.start.assert_called_once()

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
            page = PurchaseManagePage()
            with (
                patch("ui.pages.purchase_manage.ask_confirmation", return_value=True),
                patch(
                    "ui.pages.purchase_manage.get_wide_text_input",
                    return_value=("8月采购", True),
                ),
                patch(
                    "ui.pages.purchase_manage.QInputDialog.getItem",
                    return_value=("six", True),
                ),
                patch(
                    "ui.pages.purchase_manage.list_profile_entries",
                    return_value=[{"id": "six-profile", "display_name": "six"}],
                ),
                patch(
                    "ui.pages.purchase_manage.get_active_profile_id",
                    return_value="six-profile",
                ),
                patch(
                    "ui.pages.purchase_manage.load_steam_account_config_dict",
                    return_value={"steam_id": "76561198000000006"},
                ),
                patch(
                    "ui.pages.purchase_manage.load_profile_inventory_items",
                    return_value=[{"assetid": "old-asset"}],
                ),
                patch("ui.pages.purchase_manage.show_toast"),
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

    def test_recipe_tradeup_state_button_persists_and_can_be_undone(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "汰换按钮",
                profile_id="ten-profile",
                steam_id="",
                account_name="Ten",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe(1))
            card = PurchaseBatchCard(path, load_purchase_batch(path), expanded=True)

            self.assertEqual(card._recipe_tradeup_buttons[0].text(), "未汰换")
            card._recipe_tradeup_buttons[0].click()
            self.assertTrue(
                load_purchase_batch(path)["recipes"][0]["tradeup_completed"]
            )
            self.assertEqual(card._recipe_tradeup_buttons[0].text(), "已汰换")
            self.assertTrue(
                bool(card._recipe_tradeup_buttons[0].property("completed"))
            )

            card._recipe_tradeup_buttons[0].click()
            restored = load_purchase_batch(path)

        self.assertNotIn("tradeup_completed", restored["recipes"][0])
        self.assertEqual(card._recipe_tradeup_buttons[0].text(), "未汰换")

    def test_batch_card_offers_safe_departure_classification(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "离库确认",
                profile_id="ten-profile",
                steam_id="",
                account_name="Ten",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe(1))
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "ten-profile", [_inventory("departed")]
            )
            reconcile_purchase_batches_for_profile("ten-profile", [])
            card = PurchaseBatchCard(path, load_purchase_batch(path), expanded=True)
            card._filter.setCurrentText("待确认离库")
            buttons = {
                button.text() for button in card._table.findChildren(QPushButton)
            }

        self.assertIn("待确认离库 1", card._summary_label.text())
        self.assertEqual(card._table.rowCount(), 1)
        self.assertIn("卖家撤回", buttons)
        self.assertIn("正常离库", buttons)

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
            page = PurchaseManagePage()
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

    def test_purchase_batch_view_state_survives_page_refresh(self) -> None:
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
            page = PurchaseManagePage()
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
            page = PurchaseManagePage()
            with (
                patch(
                    "ui.pages.purchase_manage.list_profile_entries",
                    return_value=[
                        {"id": "wrong-profile", "display_name": "wrong"},
                        {"id": "six-profile", "display_name": "six"},
                    ],
                ),
                patch(
                    "ui.pages.purchase_manage.QInputDialog.getItem",
                    return_value=("six", True),
                ),
                patch("ui.pages.purchase_manage.ask_confirmation", return_value=True),
                patch(
                    "ui.pages.purchase_manage.load_steam_account_config_dict",
                    return_value={"steam_id": "six-steam"},
                ),
                patch(
                    "ui.pages.purchase_manage.load_profile_inventory_items",
                    return_value=[{"assetid": "six-existing"}],
                ),
                patch("ui.pages.purchase_manage.show_toast"),
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
