from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from core.alchemy_calc import (
    _substrates_display,
    _try_skin_instance_from_row,
    tradeup_average_normalized_float32,
    tradeup_product_wear_float32,
)
from core.alchemy_quality import get_name_map, get_pid_map, get_template_from_goods_name
from core.data_utils import SkinTemplate, tradeup_display_quality, wear_as_float32
from core.purchase_batches import (
    add_recipe_to_purchase_batch,
    apply_steam_tradeup_history,
    apply_purchase_batch_replacement,
    build_purchase_batch_recipe_tradeup_plan,
    compute_alchemy_ready_at,
    create_purchase_batch,
    delete_tradeup_completed_batches,
    list_ready_purchase_batch_recipes,
    list_tradeup_records,
    list_purchase_batches,
    load_purchase_batch,
    mark_all_purchase_batch_materials_ordered,
    purchase_batch_alchemy_status_text,
    purchase_batch_recipe_live_readiness,
    purchase_batch_recipe_tradeup_readiness,
    purchase_batch_section,
    purchase_batch_replacement_options,
    purchase_batch_summary,
    reconcile_purchase_batches_for_profile,
    record_inventory_recipe_tradeup_result,
    refresh_purchase_batch_alchemy_ready_at,
    record_purchase_batch_recipe_tradeup_result,
    resolve_steam_tradeup_products,
    resolve_purchase_batch_inventory_departure,
    set_purchase_batch_recipe_tradeup_completed,
    set_purchase_batch_material_status,
    toggle_all_purchase_batch_materials_ordered,
    update_purchase_batch_account,
)
from core.steam_inventory_history import parse_steam_inventory_history_page
from core.purchase_tracking import (
    STATUS_CANCELLED,
    STATUS_ORDERED,
    STATUS_PENDING,
    STATUS_RECEIVED,
)
from core.steam_tradeup import (
    build_inventory_recipe_tradeup_plan,
    inventory_recipe_tradeup_readiness,
    tradeup_plan_cached_readiness,
)


def _template_pair():
    material = next(
        template
        for template in get_name_map().values()
        if template.upper_skins and template.max_float > template.min_float
    )
    product = get_pid_map()[str(material.upper_skins[0])]
    return material, product


def _name(template) -> str:
    return (
        f"{template.weapon_name} | {template.skin_name}"
        if template.skin_name
        else template.weapon_name
    )


def _special_recipe(count: int = 10) -> dict:
    material, product = _template_pair()
    wear = wear_as_float32(
        (float(material.min_float) + float(material.max_float)) / 2.0
    )
    pairs = [(material, wear)] * count
    average = tradeup_average_normalized_float32(pairs)
    output = tradeup_product_wear_float32(average, product)
    return {
        "cost": 10.0 * count,
        "avg_nfv": float(average),
        "special_wear_output_float": output,
        "special_wear_target": {
            "paint_index": str(product.paint_index),
            "min_wear": max(float(product.min_float), output - 0.00001),
            "max_wear": min(float(product.max_float), output + 0.00001),
        },
        "substrates_display": [
            {
                "name": _name(material),
                "float_value": wear,
                "price": 10.0,
                "platform": "buff",
            }
            for _ in range(count)
        ],
        "products_display": [],
    }


def _inventory(assetid: str, wear: float | None = None) -> dict:
    material, _product = _template_pair()
    if wear is None:
        wear = wear_as_float32(
            (float(material.min_float) + float(material.max_float)) / 2.0
        )
    return {
        "assetid": assetid,
        "market_name": _name(material),
        "float": wear,
    }


class PurchaseBatchTests(unittest.TestCase):
    def test_gc_output_is_resolved_for_immediate_success_display(self) -> None:
        _material, product = _template_pair()
        wear = wear_as_float32(
            (float(product.min_float) + float(product.max_float)) / 2.0
        )
        with patch(
            "core.purchase_batches.try_build_product_price_map_from_disk",
            return_value={},
        ):
            resolved = resolve_steam_tradeup_products(
                [
                    {
                        "assetId": "output-asset",
                        "paintIndex": product.paint_index,
                        "paintWear": wear,
                    }
                ]
            )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["asset_id"], "output-asset")
        self.assertEqual(resolved[0]["paint_index"], str(product.paint_index))
        self.assertIn(_name(product), resolved[0]["name"])
        self.assertAlmostEqual(resolved[0]["float_value"], wear, places=7)

    def test_steam_history_parser_extracts_ten_inputs_and_product(self) -> None:
        material, product = _template_pair()
        descriptions = [
            {
                "appid": 730,
                "classid": "101",
                "instanceid": "0",
                "market_hash_name": next(iter(material.steam.values())),
            },
            {
                "appid": 730,
                "classid": "202",
                "instanceid": "0",
                "market_hash_name": next(iter(product.steam.values())),
            },
        ]
        inputs = "".join(
            '<a class="history_item" data-appid="730" '
            'data-classid="101" data-instanceid="0"></a>'
            for _ in range(10)
        )
        payload = {
            "descriptions": descriptions,
            "html": (
                '<div class="tradehistoryrow">'
                '<div class="tradehistory_items_plusminus">-</div>'
                f'<div class="tradehistory_items_group">{inputs}</div>'
                '<div class="tradehistory_items_plusminus">+</div>'
                '<div class="tradehistory_items_group">'
                '<a class="history_item" data-appid="730" '
                'data-classid="202" data-instanceid="0"></a>'
                '</div></div>'
            ),
        }

        events = parse_steam_inventory_history_page(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["inputs"]), 10)
        self.assertEqual(events[0]["outputs"][0]["classid"], "202")

    def test_steam_history_fills_actual_product_and_candidate_price(self) -> None:
        material, product = _template_pair()
        recipe = _special_recipe()
        recipe["products_display"] = [
            {"name": _name(product), "float_value": 0.123, "price": 88.8}
        ]
        with (
            TemporaryDirectory() as temp_dir,
            patch("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir) / "batches"),
            patch("core.purchase_batches.load_profile_inventory_items", return_value=[]),
        ):
            path = create_purchase_batch(
                "历史补全", profile_id="profile", steam_id="76561198000000001",
                account_name="账号", inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, recipe)
            mark_all_purchase_batch_materials_ordered(path)
            inventory = [_inventory(f"asset-{index}") for index in range(10)]
            reconcile_purchase_batches_for_profile("profile", inventory)
            plan = build_purchase_batch_recipe_tradeup_plan(path, entry_id)
            record_purchase_batch_recipe_tradeup_result(
                path, entry_id, input_asset_ids=plan["asset_ids"],
                output_asset_ids=["output"], gc_recipe=2,
            )
            event = {
                "inputs": [{"market_name": _name(material)} for _ in range(10)],
                "outputs": [{"market_name": _name(product), "icon_url": "icon/path"}],
            }

            self.assertEqual(apply_steam_tradeup_history("profile", [event]), 1)
            record = list_tradeup_records("profile")[0]
            self.assertEqual(apply_steam_tradeup_history("profile", [event]), 0)

        self.assertEqual(record["recipe_index"], 1)
        self.assertEqual(record["products"][0]["name"], _name(product))
        self.assertEqual(record["products"][0]["price"], 88.8)
        self.assertEqual(record["products"][0]["steam_icon_url"], "icon/path")

    def test_steam_history_recovers_manually_completed_legacy_record(self) -> None:
        material, product = _template_pair()
        recipe = _special_recipe()
        recipe["products_display"] = [
            {"name": _name(product), "float_value": 0.123, "price": 66.6}
        ]
        with (
            TemporaryDirectory() as temp_dir,
            patch("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir) / "batches"),
            patch("core.purchase_batches.load_profile_inventory_items", return_value=[]),
        ):
            path = create_purchase_batch(
                "旧版手动完成", profile_id="profile", steam_id="76561198000000001",
                account_name="账号", inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, recipe)
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "profile", [_inventory(f"legacy-{index}") for index in range(10)]
            )
            set_purchase_batch_recipe_tradeup_completed(path, entry_id, True)
            self.assertNotIn("tradeup_execution", load_purchase_batch(path)["recipes"][0])
            event = {
                "inputs": [{"market_name": _name(material)} for _ in range(10)],
                "outputs": [{"market_name": _name(product), "icon_url": "legacy/icon"}],
            }

            self.assertEqual(apply_steam_tradeup_history("profile", [event]), 1)
            stored_entry = load_purchase_batch(path)["recipes"][0]
            record = list_tradeup_records("profile")[0]

        self.assertEqual(
            stored_entry["tradeup_execution"]["method"],
            "steam_inventory_history_recovery",
        )
        self.assertEqual(len(record["products"]), 1)
        self.assertEqual(record["products"][0]["name"], _name(product))
        self.assertEqual(record["products"][0]["price"], 66.6)
        self.assertEqual(record["output_value"], 66.6)

    def test_five_covert_inventory_items_can_build_tradeup_plan(self) -> None:
        template = next(
            value for value in get_name_map().values()
            if tradeup_display_quality(value) == "隐秘" and not value.weapon_name.startswith("★")
        )
        name = _name(template)
        profile_id = "five-profile"
        steam_id = "76561198000000005"
        wear = wear_as_float32((float(template.min_float) + float(template.max_float)) / 2)
        substrates = [
            {
                "name": name, "float_value": wear, "steam_assetid": f"five-{index}",
                "steam_profile_id": profile_id, "steam_id": steam_id,
            }
            for index in range(5)
        ]
        inventory = [
            {"assetid": row["steam_assetid"], "market_name": name, "float": wear}
            for row in substrates
        ]
        with (
            patch("core.steam_tradeup.load_steam_account_config_dict", return_value={"steam_id": steam_id}),
            patch("core.steam_tradeup.load_profile_inventory_items", return_value=inventory),
        ):
            plan = build_inventory_recipe_tradeup_plan({"substrates_display": substrates})
        self.assertEqual(len(plan["asset_ids"]), 5)

    def test_inventory_recipe_tradeup_is_written_to_history(self) -> None:
        template = next(
            value for value in get_name_map().values()
            if tradeup_display_quality(value) == "隐秘" and not value.weapon_name.startswith("★")
        )
        name = _name(template)
        profile_id = "inventory-history-profile"
        wear = wear_as_float32((float(template.min_float) + float(template.max_float)) / 2)
        plan = {
            "source": "steam_inventory_recipe",
            "title": "库存五隐秘",
            "profile_id": profile_id,
            "steam_id": "76561198000000099",
            "asset_ids": [f"in-{index}" for index in range(5)],
            "materials": [
                {
                    "asset_id": f"in-{index}",
                    "name": name,
                    "float_value": wear,
                    "price": 10.0 + index,
                }
                for index in range(5)
            ],
        }
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir) / "batches",
        ), patch(
            "core.purchase_batches.list_profile_entries",
            return_value=[{"id": profile_id, "display_name": "库存号"}],
        ), patch(
            "core.purchase_batches.load_profile_inventory_items",
            return_value=[],
        ):
            record = record_inventory_recipe_tradeup_result(
                plan,
                output_asset_ids=["out-1"],
                output_items=[
                    {
                        "assetId": "out-1",
                        "paintIndex": int(template.upper_skins[0])
                        if template.upper_skins
                        else 0,
                        "paintWear": 0.2,
                    }
                ],
                gc_recipe=5,
            )
            listed = list_tradeup_records(profile_id)

        self.assertEqual(record["source"], "steam_inventory_recipe")
        self.assertEqual(record["batch_name"], "Steam 库存配方")
        self.assertEqual(record["account_name"], "库存号")
        self.assertEqual(record["material_cost"], 60.0)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["recipe_title"], "库存五隐秘")
        self.assertEqual(listed[0]["materials"][0]["asset_id"], "in-0")

    def test_received_ready_and_completed_sections_follow_inventory_cd(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir) / "batches"
        ):
            path = create_purchase_batch(
                "状态迁移", profile_id="profile", steam_id="76561198000000001",
                account_name="账号", inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            mark_all_purchase_batch_materials_ordered(path)
            inventory = [_inventory(f"asset-{index}") for index in range(10)]
            reconcile_purchase_batches_for_profile("profile", inventory)
            batch = load_purchase_batch(path)
            entry = batch["recipes"][0]
            self.assertEqual(purchase_batch_section(batch), "purchase_completed")

            future = datetime.now(timezone.utc).timestamp() + 3600
            inventory[0]["marketable"] = 0
            inventory[0]["cooldown_kind"] = "trade_hold"
            inventory[0]["cooldown_ends_at"] = future
            ready, reason = purchase_batch_recipe_live_readiness(batch, entry, inventory)
            self.assertFalse(ready)
            self.assertIn("CD", reason)

            inventory[0]["cooldown_ends_at"] = datetime.now(timezone.utc).timestamp() - 1
            with patch("core.purchase_batches.load_profile_inventory_items", return_value=inventory):
                ready_batches = list_ready_purchase_batch_recipes("profile")
            self.assertEqual(ready_batches[0][2], [entry_id])

            set_purchase_batch_recipe_tradeup_completed(path, entry_id, True)
            self.assertEqual(purchase_batch_section(load_purchase_batch(path)), "tradeup_completed")

    def test_cleanup_preserves_tradeup_statistics_history(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp_dir) / "batches"
        ):
            path = create_purchase_batch(
                "已炼金", profile_id="profile", steam_id="76561198000000001",
                account_name="账号", inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            mark_all_purchase_batch_materials_ordered(path)
            inventory = [_inventory(f"asset-{index}") for index in range(10)]
            reconcile_purchase_batches_for_profile("profile", inventory)
            plan = build_purchase_batch_recipe_tradeup_plan(path, entry_id)
            record_purchase_batch_recipe_tradeup_result(
                path, entry_id, input_asset_ids=plan["asset_ids"],
                output_asset_ids=["output"], gc_recipe=2,
            )
            self.assertEqual(delete_tradeup_completed_batches(), 1)
            self.assertFalse(path.exists())
            records = list_tradeup_records("profile")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["batch_name"], "已炼金")
        self.assertEqual(len(records[0]["materials"]), 10)

    def test_final_tradeup_preflight_rejects_missing_or_cooling_assets(self) -> None:
        plan = {
            "profile_id": "profile",
            "steam_id": "76561198000000001",
            "asset_ids": [f"asset-{index}" for index in range(10)],
        }
        inventory = [_inventory(asset_id) for asset_id in plan["asset_ids"]]
        with (
            patch("core.steam_tradeup.load_steam_account_config_dict", return_value={"steam_id": plan["steam_id"]}),
            patch("core.steam_tradeup.load_profile_inventory_items", return_value=inventory[:-1]),
        ):
            self.assertIn("不在当前库存", tradeup_plan_cached_readiness(plan)[1])
        inventory[0].update({
            "marketable": 0, "cooldown_kind": "trade_hold",
            "cooldown_ends_at": datetime.now(timezone.utc).timestamp() + 3600,
        })
        with (
            patch("core.steam_tradeup.load_steam_account_config_dict", return_value={"steam_id": plan["steam_id"]}),
            patch("core.steam_tradeup.load_profile_inventory_items", return_value=inventory),
        ):
            self.assertIn("CD", tradeup_plan_cached_readiness(plan)[1])

    def test_alchemy_result_preserves_real_inventory_identity(self) -> None:
        material, _product = _template_pair()
        wear = wear_as_float32(
            (float(material.min_float) + float(material.max_float)) / 2.0
        )
        instance = _try_skin_instance_from_row(
            {
                "goods_name": _name(material),
                "float_value": wear,
                "price": 10.0,
                "platform": "inventory",
                "steam_assetid": "asset-1",
                "steam_profile_id": "profile-1",
                "steam_id": "76561198000000001",
            }
        )

        self.assertIsNotNone(instance)
        displayed = _substrates_display([instance])[0]
        self.assertEqual(displayed["steam_assetid"], "asset-1")
        self.assertEqual(displayed["steam_profile_id"], "profile-1")
        self.assertEqual(displayed["steam_id"], "76561198000000001")

    def test_inventory_recipe_tradeup_requires_the_same_ten_live_assets(self) -> None:
        recipe = _special_recipe()
        steam_id = "76561198000000001"
        profile_id = "inventory-profile"
        inventory = []
        for index, substrate in enumerate(recipe["substrates_display"]):
            asset_id = f"inventory-asset-{index}"
            substrate.update(
                {
                    "platform": "inventory",
                    "steam_assetid": asset_id,
                    "steam_profile_id": profile_id,
                    "steam_id": steam_id,
                }
            )
            inventory.append(_inventory(asset_id, float(substrate["float_value"])))

        self.assertEqual(
            inventory_recipe_tradeup_readiness(recipe),
            (True, "真实库存材料，可先模拟再一键汰换"),
        )
        with (
            patch(
                "core.steam_tradeup.load_steam_account_config_dict",
                return_value={"steam_id": steam_id},
            ),
            patch(
                "core.steam_tradeup.load_profile_inventory_items",
                return_value=inventory,
            ),
        ):
            plan = build_inventory_recipe_tradeup_plan(recipe)

        self.assertEqual(plan["source"], "steam_inventory_recipe")
        self.assertEqual(plan["profile_id"], profile_id)
        self.assertEqual(plan["steam_id"], steam_id)
        self.assertEqual(len(set(plan["asset_ids"])), 10)

        recipe["substrates_display"][9].pop("steam_assetid")
        ready, reason = inventory_recipe_tradeup_readiness(recipe)
        self.assertFalse(ready)
        self.assertIn("资产编号", reason)

    def test_build_and_record_one_click_tradeup_plan(self) -> None:
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
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            before = load_purchase_batch(path)["recipes"][0]
            self.assertEqual(
                purchase_batch_recipe_tradeup_readiness(before),
                (False, "配方的 10 件材料全部入库后才能一键汰换"),
            )
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "tradeup-profile",
                [_inventory(f"asset-{index}") for index in range(10)],
            )

            plan = build_purchase_batch_recipe_tradeup_plan(path, entry_id)
            self.assertEqual(len(plan["asset_ids"]), 10)
            self.assertEqual(plan["steam_id"], "76561198000000001")
            self.assertEqual(len(set(plan["asset_ids"])), 10)

            with self.assertRaisesRegex(ValueError, "发生了变化"):
                record_purchase_batch_recipe_tradeup_result(
                    path,
                    entry_id,
                    input_asset_ids=list(reversed(plan["asset_ids"])),
                    output_asset_ids=["output-wrong"],
                    gc_recipe=2,
                )

            record_purchase_batch_recipe_tradeup_result(
                path,
                entry_id,
                input_asset_ids=plan["asset_ids"],
                output_asset_ids=["output-1"],
                gc_recipe=2,
            )
            recorded = load_purchase_batch(path)["recipes"][0]

        self.assertTrue(recorded["tradeup_completed"])
        self.assertEqual(recorded["tradeup_execution"]["method"], "local_steam_gc")
        self.assertEqual(recorded["tradeup_execution"]["gc_recipe"], 2)
        self.assertEqual(recorded["tradeup_execution"]["output_asset_ids"], ["output-1"])

    def test_tradeup_completed_recipe_is_ignored_by_inventory_departure_check(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "汰换状态",
                profile_id="ten-profile",
                steam_id="",
                account_name="Ten",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "ten-profile", [_inventory("consumed")]
            )
            reconcile_purchase_batches_for_profile("ten-profile", [])
            self.assertEqual(
                purchase_batch_summary(load_purchase_batch(path))["missing_review"],
                1,
            )

            self.assertTrue(
                set_purchase_batch_recipe_tradeup_completed(path, entry_id, True)
            )
            completed = load_purchase_batch(path)
            result = reconcile_purchase_batches_for_profile("ten-profile", [])

            self.assertTrue(completed["recipes"][0]["tradeup_completed"])
            self.assertTrue(completed["recipes"][0]["tradeup_completed_at"])
            self.assertNotIn(
                "inventory_missing_since",
                completed["recipes"][0]["materials"][0],
            )
            self.assertEqual(result["missing_review"], 0)
            self.assertEqual(
                purchase_batch_summary(completed)["tradeup_completed_recipes"],
                1,
            )

            self.assertTrue(
                set_purchase_batch_recipe_tradeup_completed(path, entry_id, False)
            )
            reconcile_purchase_batches_for_profile("ten-profile", [])
            restored = load_purchase_batch(path)

        self.assertNotIn("tradeup_completed", restored["recipes"][0])
        self.assertEqual(purchase_batch_summary(restored)["missing_review"], 1)

    def test_missing_received_asset_waits_for_departure_classification(self) -> None:
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
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "ten-profile", [_inventory("received-then-missing")]
            )
            original_ready = refresh_purchase_batch_alchemy_ready_at(path)

            result = reconcile_purchase_batches_for_profile("ten-profile", [])
            self.assertEqual(
                refresh_purchase_batch_alchemy_ready_at(path), original_ready
            )
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]

            self.assertEqual(result["missing_review"], 1)
            self.assertEqual(row["status"], STATUS_RECEIVED)
            self.assertTrue(row["inventory_missing_since"])

            self.assertTrue(
                resolve_purchase_batch_inventory_departure(
                    path,
                    entry_id,
                    str(row["row_id"]),
                    seller_reversed=False,
                )
            )
            result = reconcile_purchase_batches_for_profile("ten-profile", [])
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]

        self.assertEqual(result["missing_review"], 0)
        self.assertEqual(row["status"], STATUS_RECEIVED)
        self.assertEqual(
            row["normal_departure_assetid"], "received-then-missing"
        )
        self.assertEqual(batch["alchemy_ready_at"], original_ready)

    def test_seller_reversal_becomes_repurchase_and_restores_recipe(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "卖家撤回",
                profile_id="ten-profile",
                steam_id="",
                account_name="Ten",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "ten-profile", [_inventory("seller-reversed")]
            )
            refresh_purchase_batch_alchemy_ready_at(path)
            received = load_purchase_batch(path)
            self.assertEqual(
                received["recipes"][0]["recipe"]["substrates_display"][0][
                    "platform"
                ],
                "steam_inventory",
            )
            self.assertIn("alchemy_ready_at", received)

            reconcile_purchase_batches_for_profile("ten-profile", [])
            missing = load_purchase_batch(path)
            row = missing["recipes"][0]["materials"][0]
            self.assertTrue(
                resolve_purchase_batch_inventory_departure(
                    path,
                    entry_id,
                    str(row["row_id"]),
                    seller_reversed=True,
                )
            )
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]
            substrate = batch["recipes"][0]["recipe"]["substrates_display"][0]

        self.assertEqual(row["status"], STATUS_CANCELLED)
        self.assertNotIn("matched_assetid", row)
        self.assertNotIn("matched_float", row)
        self.assertNotIn("alchemy_ready_at", batch)
        self.assertEqual(substrate["platform"], "buff")
        self.assertNotIn("steam_assetid", substrate)

    def test_normal_alchemy_recipe_gets_safe_not_higher_replacements(self) -> None:
        recipe = _special_recipe()
        recipe.pop("special_wear_target", None)
        recipe.pop("special_wear_output_float", None)
        original_average = tradeup_average_normalized_float32(
            [
                (
                    get_name_map()[_name(_template_pair()[0])],
                    float(source["float_value"]),
                )
                for source in recipe["substrates_display"]
            ]
        )
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "普通炼金替代",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, recipe)
            batch = load_purchase_batch(path)
            row_id = str(batch["recipes"][0]["materials"][0]["row_id"])
            options, target_text = purchase_batch_replacement_options(
                batch,
                entry_id,
                row_id,
            )
            self.assertTrue(options)
            self.assertTrue(all(option["safe"] for option in options))
            self.assertTrue(
                all(
                    option["relation"] in {"原材料", "同产物池安全替代"}
                    for option in options
                )
            )
            safe = next(
                option
                for option in options
                if option["safe"] and not option["original"]
            )
            original_option = next(option for option in options if option["original"])
            original_wear = float(recipe["substrates_display"][0]["float_value"])
            original_template = get_template_from_goods_name(
                str(recipe["substrates_display"][0]["name"])
            )
            safe_template = get_template_from_goods_name(str(safe["name"]))
            original_normalized = SkinTemplate.float_to_normalized(
                original_wear,
                original_template.min_float,
                original_template.max_float,
            )
            safe_high_normalized = SkinTemplate.float_to_normalized(
                float(safe["max_wear"]),
                safe_template.min_float,
                safe_template.max_float,
            )
            safe = dict(safe)
            candidate_midpoint = (
                float(safe["min_wear"]) + float(safe["max_wear"])
            ) / 2.0
            prefix = math.floor(candidate_midpoint * 1000000) / 1000000
            safe["allowed_min_wear"] = float(safe["min_wear"])
            safe["allowed_max_wear"] = float(safe["max_wear"])
            safe["min_wear"] = max(float(safe["min_wear"]), prefix)
            safe["max_wear"] = min(
                float(safe["max_wear"]),
                math.nextafter(prefix + 0.000001, -math.inf),
            )
            safe["manual_wear"] = prefix
            safe["manual_wear_decimals"] = 6
            safe["manual_wear_match_mode"] = "decimal_prefix_6"
            safe["purchase_price"] = 12.34
            apply_purchase_batch_replacement(path, entry_id, row_id, safe)
            set_purchase_batch_material_status(
                path,
                entry_id,
                row_id,
                STATUS_ORDERED,
            )
            midpoint = wear_as_float32(
                (float(safe["min_wear"]) + float(safe["max_wear"])) / 2.0
            )
            result = reconcile_purchase_batches_for_profile(
                "six-profile",
                [
                    {
                        "assetid": "normal-replacement",
                        "market_name": safe["name"],
                        "float": midpoint,
                    }
                ],
            )
            batch = load_purchase_batch(path)
            updated_recipe = batch["recipes"][0]["recipe"]
            updated_row = batch["recipes"][0]["materials"][0]

        self.assertIn("产物归一化磨损不高于原配方", target_text)
        self.assertLessEqual(float(original_option["max_wear"]), original_wear)
        self.assertLessEqual(safe_high_normalized, original_normalized + 1e-6)
        self.assertEqual(updated_row["status"], STATUS_RECEIVED)
        self.assertEqual(
            updated_row["replacement"]["manual_wear_match_mode"],
            "decimal_prefix_6",
        )
        self.assertEqual(updated_row["price"], 10.0)
        self.assertEqual(updated_row["replacement"]["purchase_price"], 12.34)
        self.assertEqual(result["matched"], 1)
        self.assertLessEqual(
            wear_as_float32(float(updated_recipe["avg_nfv"])),
            wear_as_float32(original_average),
        )

    def test_mark_all_ordered_can_be_reversed(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "反选测试",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe())
            changed, target = toggle_all_purchase_batch_materials_ordered(path)
            first = purchase_batch_summary(load_purchase_batch(path))
            reverted, revert_target = toggle_all_purchase_batch_materials_ordered(path)
            second = purchase_batch_summary(load_purchase_batch(path))

        self.assertEqual((changed, target), (10, STATUS_ORDERED))
        self.assertEqual(first[STATUS_ORDERED], 10)
        self.assertEqual((reverted, revert_target), (10, STATUS_PENDING))
        self.assertEqual(second[STATUS_PENDING], 10)

    def test_reversing_all_ordered_keeps_received_materials(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "反选保留入库",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe())
            toggle_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "six-profile",
                [_inventory("received-one")],
            )
            changed, target = toggle_all_purchase_batch_materials_ordered(path)
            summary = purchase_batch_summary(load_purchase_batch(path))

        self.assertEqual((changed, target), (9, STATUS_PENDING))
        self.assertEqual(summary[STATUS_RECEIVED], 1)
        self.assertEqual(summary[STATUS_PENDING], 9)

    def test_changing_batch_account_rebuilds_baseline_and_resets_matches(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "改账号",
                profile_id="wrong-profile",
                steam_id="wrong-steam",
                account_name="wrong",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(path, _special_recipe())
            mark_all_purchase_batch_materials_ordered(path)
            reconcile_purchase_batches_for_profile(
                "wrong-profile",
                [_inventory("wrong-received")],
            )
            reset = update_purchase_batch_account(
                path,
                profile_id="six-profile",
                steam_id="six-steam",
                account_name="six",
                inventory_items=[_inventory("six-existing")],
            )
            batch = load_purchase_batch(path)
            summary = purchase_batch_summary(batch)

        self.assertEqual(reset, 1)
        self.assertEqual(batch["profile_id"], "six-profile")
        self.assertEqual(batch["baseline_asset_ids"], ["six-existing"])
        self.assertEqual(summary[STATUS_RECEIVED], 0)
        self.assertEqual(summary[STATUS_ORDERED], 10)
    def test_source_ref_blocks_only_the_same_calculation_result(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "去重测试",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            add_recipe_to_purchase_batch(
                path,
                _special_recipe(),
                source_ref="calculation-1:1",
            )
            with self.assertRaisesRegex(ValueError, "已在此采购批次"):
                add_recipe_to_purchase_batch(
                    path,
                    _special_recipe(),
                    source_ref="calculation-1:1",
                )
            add_recipe_to_purchase_batch(
                path,
                _special_recipe(),
                source_ref="calculation-1:2",
            )
            batch = load_purchase_batch(path)

        self.assertEqual(len(batch["recipes"]), 2)

    def test_twenty_recipes_report_195_received_and_five_missing(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "20配方采购",
                profile_id="six-profile",
                steam_id="",
                account_name="six",
                inventory_items=[],
            )
            for index in range(20):
                add_recipe_to_purchase_batch(
                    path, _special_recipe(), title=f"方案{index + 1}"
                )
            self.assertEqual(mark_all_purchase_batch_materials_ordered(path), 200)
            result = reconcile_purchase_batches_for_profile(
                "six-profile",
                [_inventory(f"new-{index}") for index in range(195)],
            )
            batch = load_purchase_batch(path)

        summary = purchase_batch_summary(batch)
        self.assertEqual(summary["recipes"], 20)
        self.assertEqual(summary["total"], 200)
        self.assertEqual(summary[STATUS_RECEIVED], 195)
        self.assertEqual(summary[STATUS_ORDERED], 5)
        self.assertEqual(result["matched"], 195)
        self.assertEqual(result["waiting"], 5)

    def test_batch_groups_recipes_and_reconciles_materials(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "8月采购",
                profile_id="six-profile",
                steam_id="76561198000000006",
                account_name="six",
                inventory_items=[_inventory("old")],
            )
            add_recipe_to_purchase_batch(path, _special_recipe(), title="方案1")
            add_recipe_to_purchase_batch(path, _special_recipe(), title="方案2")
            self.assertEqual(mark_all_purchase_batch_materials_ordered(path), 20)

            result = reconcile_purchase_batches_for_profile(
                "six-profile",
                [_inventory("old"), *[_inventory(f"new-{i}") for i in range(20)]],
            )
            batch = load_purchase_batch(path)

        self.assertEqual(result["matched"], 20)
        self.assertEqual(result["waiting"], 0)
        summary = purchase_batch_summary(batch)
        self.assertEqual(summary["recipes"], 2)
        self.assertEqual(summary[STATUS_RECEIVED], 20)
        self.assertEqual(len(list(result["used_asset_ids"])), 20)

    def test_one_asset_is_not_reused_between_batch_rows(self) -> None:
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
            add_recipe_to_purchase_batch(path, _special_recipe(2))
            mark_all_purchase_batch_materials_ordered(path)
            result = reconcile_purchase_batches_for_profile(
                "six-profile", [_inventory("only-one")]
            )
            batch = load_purchase_batch(path)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(purchase_batch_summary(batch)[STATUS_RECEIVED], 1)

    def test_failed_batch_write_is_not_reported_as_received(self) -> None:
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
            add_recipe_to_purchase_batch(path, _special_recipe(1))
            mark_all_purchase_batch_materials_ordered(path)
            with patch(
                "core.purchase_batches._write_batch",
                side_effect=OSError("disk full"),
            ):
                result = reconcile_purchase_batches_for_profile(
                    "six-profile", [_inventory("new")]
                )
            batch = load_purchase_batch(path)

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(result["save_failures"], 1)
        self.assertEqual(purchase_batch_summary(batch)[STATUS_ORDERED], 1)

    def test_gap_recommends_and_accepts_safe_replacement_range(self) -> None:
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
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]
            row_id = str(row["row_id"])
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_CANCELLED
            )
            batch = load_purchase_batch(path)
            options, target_text = purchase_batch_replacement_options(
                batch, entry_id, row_id
            )
            self.assertTrue(options)
            self.assertTrue(all(option["safe"] for option in options))
            self.assertTrue(
                all(
                    option["relation"] in {"原材料", "同产物池安全替代"}
                    for option in options
                )
            )
            safe = next(option for option in options if option["safe"])
            safe["purchase_price"] = 9.87
            actual_wear = wear_as_float32(
                (float(safe["min_wear"]) + float(safe["max_wear"])) / 2.0
            )
            prefix = math.floor(actual_wear * 1_000_000) / 1_000_000
            safe["manual_wear"] = prefix
            safe["manual_wear_decimals"] = 6
            apply_purchase_batch_replacement(path, entry_id, row_id, safe)
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            result = reconcile_purchase_batches_for_profile(
                "six-profile",
                [
                    {
                        "assetid": "replacement",
                        "market_name": safe["name"],
                        "float": actual_wear,
                    }
                ],
            )
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]

        self.assertIn("目标产物磨损", target_text)
        self.assertEqual(row["status"], STATUS_RECEIVED)
        self.assertEqual(row["matched_assetid"], "replacement")
        self.assertEqual(result["matched"], 1)

    def test_second_replacement_is_revalidated_after_first_actual_wear(self) -> None:
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
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe())
            batch = load_purchase_batch(path)
            rows = batch["recipes"][0]["materials"][:2]
            planned: list[tuple[dict, dict]] = []
            for row in rows:
                current = load_purchase_batch(path)
                candidates, _target = purchase_batch_replacement_options(
                    current, entry_id, str(row["row_id"])
                )
                option = next(candidate for candidate in candidates if candidate["original"])
                planned.append((row, option))
            high = min(float(option["max_wear"]) for _row, option in planned)
            actual_wear = wear_as_float32(high)
            if actual_wear > high:
                actual_wear = float(
                    np.nextafter(np.float32(actual_wear), np.float32(-math.inf))
                )
            prefix = math.floor(actual_wear * 1_000_000) / 1_000_000
            for row, option in planned:
                option["purchase_price"] = 8.76
                option["manual_wear"] = prefix
                option["manual_wear_decimals"] = 6
                apply_purchase_batch_replacement(
                    path, entry_id, str(row["row_id"]), option
                )
                set_purchase_batch_material_status(
                    path, entry_id, str(row["row_id"]), STATUS_ORDERED
                )
            result = reconcile_purchase_batches_for_profile(
                "six-profile",
                [
                    _inventory("replacement-1", actual_wear),
                    _inventory("replacement-2", actual_wear),
                ],
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["waiting"], 1)

    def test_reconcile_matches_inventory_by_six_decimal_wear_prefix(self) -> None:
        from core.purchase_tracking import inventory_wear_matches_planned

        self.assertTrue(
            inventory_wear_matches_planned(
                0.014719970524311066,
                0.014720000326633453,
            )
        )
        self.assertTrue(
            inventory_wear_matches_planned(
                0.003004809841513634,
                0.003005000064149499,
            )
        )
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            recipe = _special_recipe()
            recipe["substrates_display"][0]["float_value"] = 0.014719970524311066
            path = create_purchase_batch(
                "前缀匹配",
                profile_id="prefix-profile",
                steam_id="",
                account_name="prefix",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, recipe)
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]
            set_purchase_batch_material_status(
                path, entry_id, str(row["row_id"]), STATUS_ORDERED
            )
            result = reconcile_purchase_batches_for_profile(
                "prefix-profile",
                [_inventory("steam-1", 0.014720000326633453)],
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["waiting"], 0)

    def test_repurchase_can_match_an_asset_after_undoing_received(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "撤销后重新补购",
                profile_id="repurchase-profile",
                steam_id="",
                account_name="repurchase",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            row = load_purchase_batch(path)["recipes"][0]["materials"][0]
            row_id = str(row["row_id"])
            inventory = [_inventory("same-steam-asset", float(row["float_value"]))]

            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            first = reconcile_purchase_batches_for_profile(
                "repurchase-profile", inventory
            )
            self.assertEqual(first["matched"], 1)

            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            protected = load_purchase_batch(path)["recipes"][0]["materials"][0]
            self.assertIn("same-steam-asset", protected["ignored_asset_ids"])
            immediate = reconcile_purchase_batches_for_profile(
                "repurchase-profile", inventory
            )
            self.assertEqual(immediate["matched"], 0)

            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_CANCELLED
            )
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_PENDING
            )
            fresh = load_purchase_batch(path)["recipes"][0]["materials"][0]
            self.assertNotIn("ignored_asset_ids", fresh)
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            retried = reconcile_purchase_batches_for_profile(
                "repurchase-profile", inventory
            )
            received = load_purchase_batch(path)["recipes"][0]["materials"][0]

        self.assertEqual(retried["matched"], 1)
        self.assertEqual(received["status"], STATUS_RECEIVED)
        self.assertEqual(received["matched_assetid"], "same-steam-asset")

    def test_reselecting_replacement_clears_the_undone_asset_guard(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "撤销后重选替代品",
                profile_id="replacement-retry-profile",
                steam_id="",
                account_name="replacement-retry",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            row = load_purchase_batch(path)["recipes"][0]["materials"][0]
            row_id = str(row["row_id"])
            actual_wear = float(row["float_value"])
            inventory = [_inventory("same-replacement-asset", actual_wear)]
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            reconcile_purchase_batches_for_profile(
                "replacement-retry-profile", inventory
            )
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )

            options, _target = purchase_batch_replacement_options(
                load_purchase_batch(path), entry_id, row_id
            )
            original = dict(next(option for option in options if option["original"]))
            original["purchase_price"] = 10.0
            original["manual_wear"] = math.floor(actual_wear * 1_000_000) / 1_000_000
            original["manual_wear_decimals"] = 6
            apply_purchase_batch_replacement(path, entry_id, row_id, original)
            reset = load_purchase_batch(path)["recipes"][0]["materials"][0]

            self.assertNotIn("ignored_asset_ids", reset)
            set_purchase_batch_material_status(
                path, entry_id, row_id, STATUS_ORDERED
            )
            result = reconcile_purchase_batches_for_profile(
                "replacement-retry-profile", inventory
            )
            received = load_purchase_batch(path)["recipes"][0]["materials"][0]

        self.assertEqual(result["matched"], 1)
        self.assertEqual(received["status"], STATUS_RECEIVED)
        self.assertEqual(received["matched_assetid"], "same-replacement-asset")

    def test_alchemy_ready_at_is_next_hour_plus_seven_days(self) -> None:
        verified = datetime(2026, 8, 27, 9, 34, tzinfo=timezone(timedelta(hours=8)))
        ready = compute_alchemy_ready_at(verified)
        self.assertEqual(
            ready,
            datetime(2026, 9, 3, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(
            purchase_batch_alchemy_status_text(
                {
                    "alchemy_ready_at": ready.isoformat(),
                    "recipes": [
                        {
                            "materials": [
                                {"status": STATUS_RECEIVED},
                            ]
                        }
                    ],
                },
                now=verified,
            ),
            "炼金时间:2026-9-3 10:00",
        )
        self.assertEqual(
            purchase_batch_alchemy_status_text(
                {
                    "alchemy_ready_at": ready.isoformat(),
                    "recipes": [
                        {
                            "materials": [
                                {"status": STATUS_RECEIVED},
                            ]
                        }
                    ],
                },
                now=ready,
            ),
            "可炼金",
        )

    def test_refresh_alchemy_ready_at_only_when_fully_received(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "炼金时间",
                profile_id="alchemy-profile",
                steam_id="",
                account_name="alchemy",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]
            set_purchase_batch_material_status(
                path, entry_id, str(row["row_id"]), STATUS_ORDERED
            )
            verified = datetime(2026, 8, 27, 9, 34, tzinfo=timezone(timedelta(hours=8)))
            self.assertIsNone(
                refresh_purchase_batch_alchemy_ready_at(path, verified_at=verified)
            )
            self.assertNotIn("alchemy_ready_at", load_purchase_batch(path))

            inventory = _inventory("ready-1", float(row["float_value"]))
            result = reconcile_purchase_batches_for_profile(
                "alchemy-profile", [inventory]
            )
            self.assertEqual(result["matched"], 1)
            ready_iso = refresh_purchase_batch_alchemy_ready_at(
                path, verified_at=verified
            )
            updated = load_purchase_batch(path)

        self.assertIsNotNone(ready_iso)
        self.assertEqual(
            purchase_batch_alchemy_status_text(updated, now=verified),
            "炼金时间:2026-9-3 10:00",
        )

    def test_refresh_alchemy_ready_at_is_not_moved_after_it_is_set(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            path = create_purchase_batch(
                "炼金时间锁定",
                profile_id="alchemy-lock-profile",
                steam_id="",
                account_name="alchemy",
                inventory_items=[],
            )
            entry_id = add_recipe_to_purchase_batch(path, _special_recipe(1))
            batch = load_purchase_batch(path)
            row = batch["recipes"][0]["materials"][0]
            set_purchase_batch_material_status(
                path, entry_id, str(row["row_id"]), STATUS_ORDERED
            )
            first_verified = datetime(
                2026, 8, 27, 9, 34, tzinfo=timezone(timedelta(hours=8))
            )
            reconcile_purchase_batches_for_profile(
                "alchemy-lock-profile",
                [_inventory("ready-lock-1", float(row["float_value"]))],
            )
            first_ready = refresh_purchase_batch_alchemy_ready_at(
                path, verified_at=first_verified
            )
            later_verified = datetime(
                2026, 8, 27, 11, 20, tzinfo=timezone(timedelta(hours=8))
            )
            second_ready = refresh_purchase_batch_alchemy_ready_at(
                path, verified_at=later_verified
            )
            updated = load_purchase_batch(path)

        self.assertEqual(first_ready, second_ready)
        self.assertEqual(
            purchase_batch_alchemy_status_text(updated, now=first_verified),
            "炼金时间:2026-9-3 10:00",
        )

    def test_list_batches_is_newest_first(self) -> None:
        with TemporaryDirectory() as temp_dir, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR",
            Path(temp_dir),
        ):
            create_purchase_batch(
                "第一批",
                profile_id="one",
                steam_id="",
                account_name="one",
                inventory_items=[],
            )
            create_purchase_batch(
                "第二批",
                profile_id="two",
                steam_id="",
                account_name="two",
                inventory_items=[],
            )
            names = [payload["name"] for _path, payload in list_purchase_batches()]
        self.assertEqual(names, ["第二批", "第一批"])


if __name__ == "__main__":
    unittest.main()
