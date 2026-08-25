from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.alchemy_quality import get_name_map
from core.data_utils import wear_as_float32
from core.purchase_tracking import (
    STATUS_ORDERED,
    STATUS_RECEIVED,
    mark_all_materials_ordered,
    recipe_substrate_inventory_key,
    purchase_tracking,
    reconcile_saved_recipes_for_profile,
    start_purchase_batch,
)


def _material_fixture() -> tuple[str, float]:
    for template in get_name_map().values():
        if template.upper_skins and template.max_float > template.min_float:
            name = (
                f"{template.weapon_name} | {template.skin_name}"
                if template.skin_name
                else template.weapon_name
            )
            wear = wear_as_float32(
                (float(template.min_float) + float(template.max_float)) / 2.0
            )
            return name, wear
    raise AssertionError("没有可用的炼金材料模板")


def _recipe(count: int = 1) -> dict:
    name, wear = _material_fixture()
    return {
        "substrates_display": [
            {
                "name": name,
                "float_value": wear,
                "price": 10.0,
                "platform": "buff",
            }
            for _ in range(count)
        ]
    }


def _inventory(assetid: str, *, wear_offset: float = 0.0) -> dict:
    name, wear = _material_fixture()
    return {
        "assetid": assetid,
        "market_name": name,
        "float": wear_as_float32(wear + wear_offset),
    }


class PurchaseTrackingTests(unittest.TestCase):
    def test_same_paint_index_on_different_weapons_has_different_key(self) -> None:
        templates = {
            "first": SimpleNamespace(
                weapon_name="AK-47",
                skin_name="渐变之色",
                paint_index=38,
                stat_trak=False,
            ),
            "second": SimpleNamespace(
                weapon_name="M4A4",
                skin_name="渐变之色",
                paint_index=38,
                stat_trak=False,
            ),
        }

        with patch(
            "core.purchase_tracking.get_template_from_goods_name",
            side_effect=lambda name: templates.get(name),
        ):
            first_key = recipe_substrate_inventory_key(
                {"name": "first", "float_value": 0.1}
            )
            second_key = recipe_substrate_inventory_key(
                {"name": "second", "float_value": 0.1}
            )

        self.assertNotEqual(first_key, second_key)

    def test_baseline_inventory_is_not_counted_as_new_delivery(self) -> None:
        recipe = _recipe(2)
        start_purchase_batch(
            recipe,
            profile_id="six-profile",
            steam_id="76561198000000006",
            account_name="six",
            inventory_items=[_inventory("old-asset")],
        )
        self.assertEqual(mark_all_materials_ordered(recipe), 2)

        with (
            patch(
                "core.purchase_tracking.list_saved_recipes",
                return_value=[(Path("recipe.json"), {"recipe": recipe})],
            ),
            patch("core.purchase_tracking.update_recipe_recipe_dict") as save,
        ):
            result = reconcile_saved_recipes_for_profile(
                "six-profile",
                [
                    _inventory("old-asset"),
                    _inventory("new-asset-1"),
                    _inventory("new-asset-2"),
                ],
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["waiting"], 0)
        saved_recipe = save.call_args.args[1]
        tracking = purchase_tracking(saved_recipe)
        self.assertIsNotNone(tracking)
        rows = tracking["materials"]
        self.assertEqual({row["status"] for row in rows}, {STATUS_RECEIVED})
        self.assertEqual(
            {row["matched_assetid"] for row in rows},
            {"new-asset-1", "new-asset-2"},
        )
        save.assert_called_once()

    def test_one_inventory_asset_cannot_fill_two_recipe_rows(self) -> None:
        first = _recipe()
        second = _recipe()
        for recipe in (first, second):
            start_purchase_batch(
                recipe,
                profile_id="six-profile",
                steam_id="76561198000000006",
                account_name="six",
                inventory_items=[],
            )
            mark_all_materials_ordered(recipe)

        with (
            patch(
                "core.purchase_tracking.list_saved_recipes",
                return_value=[
                    (Path("first.json"), {"recipe": first}),
                    (Path("second.json"), {"recipe": second}),
                ],
            ),
            patch("core.purchase_tracking.update_recipe_recipe_dict") as save,
        ):
            result = reconcile_saved_recipes_for_profile(
                "six-profile",
                [_inventory("only-new-asset")],
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["waiting"], 1)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], Path("first.json"))
        saved_recipe = save.call_args.args[1]
        self.assertEqual(
            purchase_tracking(saved_recipe)["materials"][0]["status"],
            STATUS_RECEIVED,
        )

    def test_different_wear_does_not_match(self) -> None:
        recipe = _recipe()
        start_purchase_batch(
            recipe,
            profile_id="six-profile",
            steam_id="76561198000000006",
            account_name="six",
            inventory_items=[],
        )
        mark_all_materials_ordered(recipe)

        with (
            patch(
                "core.purchase_tracking.list_saved_recipes",
                return_value=[(Path("recipe.json"), {"recipe": recipe})],
            ),
            patch("core.purchase_tracking.update_recipe_recipe_dict"),
        ):
            result = reconcile_saved_recipes_for_profile(
                "six-profile",
                [_inventory("wrong-wear", wear_offset=0.001)],
            )

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(
            purchase_tracking(recipe)["materials"][0]["status"],
            STATUS_ORDERED,
        )

    def test_failed_recipe_write_is_not_reported_as_received(self) -> None:
        recipe = _recipe()
        start_purchase_batch(
            recipe,
            profile_id="six-profile",
            steam_id="76561198000000006",
            account_name="six",
            inventory_items=[],
        )
        mark_all_materials_ordered(recipe)

        with (
            patch(
                "core.purchase_tracking.list_saved_recipes",
                return_value=[(Path("recipe.json"), {"recipe": recipe})],
            ),
            patch(
                "core.purchase_tracking.update_recipe_recipe_dict",
                side_effect=OSError("disk full"),
            ),
        ):
            result = reconcile_saved_recipes_for_profile(
                "six-profile",
                [_inventory("new-asset")],
            )

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(result["save_failures"], 1)
        self.assertEqual(
            purchase_tracking(recipe)["materials"][0]["status"],
            STATUS_ORDERED,
        )


if __name__ == "__main__":
    unittest.main()
