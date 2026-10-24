from __future__ import annotations

import math
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.data_utils import SkinTemplate, wear_as_float32
from core.purchase_batches import (
    _product_repurchase_ceiling,
    add_recipe_to_purchase_batch,
    apply_purchase_batch_replacement,
    create_purchase_batch,
    load_purchase_batch,
    preview_purchase_batch_replacement,
    purchase_batch_replacement_options,
    reconcile_purchase_batches_for_profile,
    set_purchase_batch_material_status,
)
from core.purchase_tracking import STATUS_ORDERED, STATUS_RECEIVED


@contextmanager
def replacement_batch(count=10, wear=0.69):
    quality = "隐秘" if count == 5 else "保密"
    output_quality = "非凡" if count == 5 else "隐秘"
    material = SkinTemplate("m", "Material", "A", quality, False, upper_skins=["p1", "p2"])
    alternative = SkinTemplate("a", "Material", "B", quality, False, min_float=0.1,
                               max_float=0.9, upper_skins=["p1", "p2"])
    first = SkinTemplate("p1", "Product", "A", output_quality, False)
    # At baseline 0.69 this product is 0.0695: its 0.07 appearance
    # boundary is tighter than the first product's 0.76 collection boundary.
    second = SkinTemplate("p2", "Product", "B", output_quality, False,
                          min_float=0.0005, max_float=0.1005)
    names = {f"{t.weapon_name} | {t.skin_name}": t for t in (material, alternative, first, second)}
    outputs = {"p1": first, "p2": second}
    with TemporaryDirectory() as temp, ExitStack() as stack:
        for target, value in (
            ("core.purchase_batches.PURCHASE_BATCHES_DIR", Path(temp)),
            ("core.alchemy_quality.get_name_map", lambda: names),
            ("core.purchase_batches.get_name_map", lambda: names),
            ("core.purchase_batches.get_pid_map", lambda: outputs),
            ("core.alchemy_calc.get_pid_map", lambda: outputs),
        ):
            stack.enter_context(patch(target, value))
        path = create_purchase_batch("补买", profile_id="profile", steam_id="", account_name="test", inventory_items=[])
        recipe = {"substrates_display": [dict(name="Material | A", float_value=wear, price=10) for _ in range(count)]}
        entry_id = add_recipe_to_purchase_batch(path, recipe)
        yield path, entry_id


def replacement_option(path, entry_id, index, wear):
    batch = load_purchase_batch(path)
    row = batch["recipes"][0]["materials"][index]
    options, _ = purchase_batch_replacement_options(batch, entry_id, row["row_id"])
    option = dict(next(candidate for candidate in options if candidate["original"]))
    option.update(manual_wear=wear, manual_wear_decimals=6, purchase_price=10)
    return row["row_id"], option


def confirm(path, entry_id, row_id, option):
    preview = preview_purchase_batch_replacement(load_purchase_batch(path), entry_id, row_id, option)
    option.update(outcome_preview_confirmed=True, outcome_preview_fingerprint=preview["fingerprint"])
    apply_purchase_batch_replacement(path, entry_id, row_id, option)
    return preview


class PurchaseReplacementTests(unittest.TestCase):
    def test_ceilings_follow_collection_wear_intervals(self):
        for wear, boundary in (
            (0.009, 0.01), (0.019, 0.02), (0.029, 0.03), (0.039, 0.04),
            (0.049, 0.07), (0.079, 0.08), (0.115, 0.15),
            (0.179, 0.18), (0.20, 0.21), (0.26, 0.27),
            (0.37, 0.38), (0.44, 0.45), (0.69, 0.76), (0.99, 1.0),
            (0.5, 0.63),
        ):
            with self.subTest(wear=wear):
                ceiling = _product_repurchase_ceiling(wear)
                self.assertLess(ceiling, boundary)
                self.assertGreaterEqual(ceiling, wear)
        self.assertEqual(_product_repurchase_ceiling(1.0), 1.0)

    def test_pending_low_wear_releases_budget_for_another_purchase_in_both_sizes(self):
        for count in (5, 10):
            with self.subTest(count=count), replacement_batch(count) as (path, entry_id):
                _, before = replacement_option(path, entry_id, 1, 0.8)
                first_id, low = replacement_option(path, entry_id, 0, 0.59)
                confirm(path, entry_id, first_id, low)
                self.assertNotEqual(load_purchase_batch(path)["recipes"][0]["materials"][0]["status"], STATUS_RECEIVED)
                second_id, after = replacement_option(path, entry_id, 1, 0.8)
                self.assertGreater(after["max_wear"], before["max_wear"] + 0.09)
                self.assertGreater(after["max_wear"], 0.79)
                # Both inputs combined keep every product inside its original buckets.
                preview = confirm(path, entry_id, second_id, after)
                self.assertEqual(preview["count"], count)
                self.assertTrue(all(p["changes"] == "区间未变化" for p in preview["products"]))
                for row_id in (first_id, second_id):
                    set_purchase_batch_material_status(path, entry_id, row_id, STATUS_ORDERED)
                result = reconcile_purchase_batches_for_profile("profile", [
                    dict(assetid="low", market_name="Material | A", float=wear_as_float32(0.5900005)),
                    dict(assetid="high", market_name="Material | A", float=wear_as_float32(0.8000005)),
                ])
                self.assertEqual(result["matched"], 2)

    def test_0179_output_is_capped_at_018_and_preview_detects_crossing(self):
        with replacement_batch(wear=0.179) as (path, entry_id):
            row_id, option = replacement_option(path, entry_id, 0, 0.1889)
            self.assertTrue(option["recommended_available"])
            self.assertGreater(option["max_wear"], 0.188)
            self.assertLess(option["max_wear"], 0.190)
            inside = preview_purchase_batch_replacement(load_purchase_batch(path), entry_id, row_id, option)
            first = next(product for product in inside["products"] if product["name"] == "Product | A")
            self.assertLess(first["max_wear"], 0.18)
            self.assertEqual(first["changes"], "区间未变化")
            option["manual_wear"] = 0.1891
            outside = preview_purchase_batch_replacement(load_purchase_batch(path), entry_id, row_id, option)
            first = next(product for product in outside["products"] if product["name"] == "Product | A")
            self.assertGreater(first["min_wear"], 0.18)
            self.assertIn("磨损区间变化", first["changes"])
            self.assertNotIn("外观品级变化", first["changes"])

    def test_recommendation_obeys_the_tightest_product_not_any_single_product(self):
        with replacement_batch() as (path, entry_id):
            _, option = replacement_option(path, entry_id, 0, 0.69)
            self.assertTrue(option["recommended_available"])
            self.assertGreater(option["max_wear"], 0.73)
            self.assertLess(option["max_wear"], 0.75)
            self.assertLess(option["max_wear"], 0.79)

    def test_confirmed_wear_above_recommendation_remains_matchable(self):
        with replacement_batch() as (path, entry_id):
            row_id, option = replacement_option(path, entry_id, 0, 0.9)
            self.assertGreater(option["manual_wear"], option["max_wear"])
            preview = confirm(path, entry_id, row_id, option)
            self.assertTrue(any(p["changes"] != "区间未变化" for p in preview["products"]))
            set_purchase_batch_material_status(path, entry_id, row_id, STATUS_ORDERED)
            result = reconcile_purchase_batches_for_profile("profile", [
                dict(assetid="accepted", market_name="Material | A", float=wear_as_float32(0.9000005))
            ])
            self.assertEqual(result["matched"], 1)

    def test_preview_is_invalidated_when_another_purchase_changes(self):
        with replacement_batch() as (path, entry_id):
            row_id, option = replacement_option(path, entry_id, 0, 0.7)
            preview = preview_purchase_batch_replacement(load_purchase_batch(path), entry_id, row_id, option)
            option.update(outcome_preview_confirmed=True, outcome_preview_fingerprint=preview["fingerprint"])
            other_id, other = replacement_option(path, entry_id, 1, 0.59)
            confirm(path, entry_id, other_id, other)
            with self.assertRaisesRegex(ValueError, "重新预览"):
                apply_purchase_batch_replacement(path, entry_id, row_id, option)

    def test_physical_limits_and_prefix_precision_still_apply(self):
        with replacement_batch() as (path, entry_id):
            row_id, option = replacement_option(path, entry_id, 0, 1.000001)
            for invalid in (1.000001, -0.000001, 0.1234567, math.nan):
                with self.subTest(wear=invalid), self.assertRaises(ValueError):
                    option["manual_wear"] = invalid
                    apply_purchase_batch_replacement(path, entry_id, row_id, option)
