from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from core.alchemy_calc import (
    tradeup_average_normalized_float32,
    tradeup_product_wear_float32,
)
from core.alchemy_quality import get_name_map, get_pid_map, get_template_from_goods_name
from core.data_utils import SkinTemplate, wear_as_float32
from core.purchase_batches import (
    add_recipe_to_purchase_batch,
    apply_purchase_batch_replacement,
    compute_alchemy_ready_at,
    create_purchase_batch,
    list_purchase_batches,
    load_purchase_batch,
    mark_all_purchase_batch_materials_ordered,
    purchase_batch_alchemy_status_text,
    purchase_batch_replacement_options,
    purchase_batch_summary,
    reconcile_purchase_batches_for_profile,
    refresh_purchase_batch_alchemy_ready_at,
    set_purchase_batch_material_status,
    toggle_all_purchase_batch_materials_ordered,
    update_purchase_batch_account,
)
from core.purchase_tracking import (
    STATUS_CANCELLED,
    STATUS_ORDERED,
    STATUS_PENDING,
    STATUS_RECEIVED,
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
