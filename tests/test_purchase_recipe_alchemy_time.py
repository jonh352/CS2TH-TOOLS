from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel

from core import purchase_batches as purchases
from core.purchase_tracking import STATUS_CANCELLED, STATUS_ORDERED
from tests.test_purchase_batches import _special_recipe, _inventory
from ui.widgets.purchase_batch_card import PurchaseBatchCard


TZ = timezone(timedelta(hours=8))


class PurchaseRecipeAlchemyTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.inventory = []
        for target, kwargs in (
            ("PURCHASE_BATCHES_DIR", {"new": Path(self.directory.name)}),
            ("load_profile_inventory_items", {"return_value": self.inventory}),
        ):
            patcher = patch.object(purchases, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = purchases.create_purchase_batch("Steam 时间", profile_id="p", steam_id="", account_name="p", inventory_items=[])

    def receive_recipe(self, ends):
        entry_id = purchases.add_recipe_to_purchase_batch(self.path, _special_recipe(len(ends)))
        entry = next(row for row in purchases.load_purchase_batch(self.path)["recipes"] if row["id"] == entry_id)
        for row, end in zip(entry["materials"], ends):
            purchases.set_purchase_batch_material_status(self.path, entry_id, row["row_id"], STATUS_ORDERED)
            item = _inventory(str(10000 + len(self.inventory)), row["float_value"])
            item.update(marketable=1 if end is None else 0, cooldown_kind=None if end is None else "trade_hold", cooldown_ends_at=None if end is None else end.timestamp())
            self.inventory.append(item)
        purchases.reconcile_purchase_batches_for_profile("p", self.inventory)
        return entry_id

    def test_latest_steam_time_next_hour_not_receipt_time(self):
        first = datetime(2026, 10, 24, 9, 14, tzinfo=TZ)
        last = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        self.receive_recipe([first] * 9 + [last])
        batch = purchases.load_purchase_batch(self.path)
        entry = batch["recipes"][0]
        ready = datetime.fromisoformat(entry["alchemy_ready_at"])
        self.assertEqual(ready, datetime(2026, 10, 25, 23, tzinfo=TZ))
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(entry, now=last), "炼金时间:2026-10-25 23:00")
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(entry, now=ready), "可炼金")
        entry["materials"][-1]["received_at"] = "2030-01-01T00:00:00+00:00"
        purchases._write_batch(self.path, batch)
        self.assertEqual(purchases.load_purchase_batch(self.path)["recipes"][0]["alchemy_ready_at"], ready.isoformat())
        self.inventory.append({"assetid": "unrelated", "cooldown_ends_at": (last + timedelta(days=30)).timestamp()})
        self.assertEqual(purchases.refresh_purchase_batch_alchemy_ready_at(self.path, verified_at=last + timedelta(days=90)), ready.isoformat())

    def test_recipes_keep_independent_clocks_and_card_shows_each_one(self):
        first = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        later = first + timedelta(days=1, hours=2)
        self.receive_recipe([first] * 10)
        self.receive_recipe([later] * 10)
        batch = purchases.load_purchase_batch(self.path)
        self.assertEqual(datetime.fromisoformat(batch["recipes"][0]["alchemy_ready_at"]), datetime(2026, 10, 25, 23, tzinfo=TZ))
        self.assertEqual(datetime.fromisoformat(batch["recipes"][1]["alchemy_ready_at"]), datetime(2026, 10, 27, 1, tzinfo=TZ))
        self.assertEqual(batch["alchemy_ready_at"], batch["recipes"][1]["alchemy_ready_at"])
        with patch("ui.widgets.purchase_batch_card.purchase_batch_recipe_alchemy_status_text", side_effect=lambda entry: purchases.purchase_batch_recipe_alchemy_status_text(entry, now=first)):
            card = PurchaseBatchCard(self.path, batch, expanded=True)
        self.addCleanup(card.deleteLater)
        labels = card.findChildren(QLabel, "purchaseRecipeAlchemyTime")
        self.assertEqual({label.text() for label in labels}, {"炼金时间:2026-10-25 23:00", "炼金时间:2026-10-27 01:00"})

    def test_loading_keeps_saved_clock_until_explicit_inventory_check(self):
        last = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        self.receive_recipe([last] * 10)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for entry in raw["recipes"]:
            entry["alchemy_ready_at"] = "2030-01-01T00:00:00+00:00"
            entry.pop("alchemy_time_source", None)
            for row in entry["materials"]:
                row.pop("steam_cooldown", None)
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        updated = purchases.load_purchase_batch(self.path)
        self.assertEqual(updated["recipes"][0]["alchemy_ready_at"], "2030-01-01T00:00:00+00:00")
        purchases.refresh_purchase_batch_alchemy_ready_at(self.path)
        updated = purchases.load_purchase_batch(self.path)
        self.assertEqual(datetime.fromisoformat(updated["recipes"][0]["alchemy_ready_at"]), datetime(2026, 10, 25, 23, tzinfo=TZ))
        self.inventory[-1]["cooldown_ends_at"] = (last + timedelta(hours=2)).timestamp()
        updated = purchases.load_purchase_batch(self.path)
        self.assertEqual(datetime.fromisoformat(updated["recipes"][0]["alchemy_ready_at"]), datetime(2026, 10, 25, 23, tzinfo=TZ))
        purchases.refresh_purchase_batch_alchemy_ready_at(self.path)
        updated = purchases.load_purchase_batch(self.path)
        self.assertEqual(datetime.fromisoformat(updated["recipes"][0]["alchemy_ready_at"]), datetime(2026, 10, 26, 1, tzinfo=TZ))

    def test_repurchase_clears_affected_clock_only(self):
        last = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        first_id = self.receive_recipe([last] * 10)
        self.receive_recipe([last + timedelta(days=2)] * 10)
        before = purchases.load_purchase_batch(self.path)
        row_id = before["recipes"][0]["materials"][0]["row_id"]
        purchases.set_purchase_batch_material_status(self.path, first_id, row_id, STATUS_CANCELLED)
        after = purchases.load_purchase_batch(self.path)
        self.assertNotIn("alchemy_ready_at", after)
        self.assertNotIn("alchemy_ready_at", after["recipes"][0])
        self.assertNotIn("steam_cooldown", after["recipes"][0]["materials"][0])
        self.assertEqual(after["recipes"][1]["alchemy_ready_at"], before["recipes"][1]["alchemy_ready_at"])

    def test_all_clear_materials_display_ready_without_guessing_future_date(self):
        self.receive_recipe([None] * 10)
        batch = purchases.load_purchase_batch(self.path)
        self.assertNotIn("alchemy_ready_at", batch)
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(batch["recipes"][0]), "可炼金")

    def test_expanding_recipe_uses_saved_clock_without_inventory_check(self):
        last = datetime(2030, 1, 1, 19, 12, tzinfo=TZ)
        self.receive_recipe([last] * 10)
        card = PurchaseBatchCard(self.path, purchases.load_purchase_batch(self.path), expanded=True)
        self.addCleanup(card.deleteLater)
        card.show()
        self.addCleanup(card.close)
        self.app.processEvents()
        label = card.findChild(QLabel, "purchaseRecipeAlchemyTime")
        self.assertEqual(label.text(), "炼金时间:2030-1-1 20:00")
        original_groups = tuple(card._detail_widgets)
        for item in self.inventory:
            item.update(marketable=1, cooldown_kind=None, cooldown_ends_at=None)
        with patch.object(purchases, "load_profile_inventory_items", side_effect=AssertionError("展开不应读取库存")):
            card._recipe_group_buttons[0].click()
            purchases.load_purchase_batch(self.path)
        self.assertEqual(label.text(), "炼金时间:2030-1-1 20:00")
        self.assertFalse(hasattr(card, "_alchemy_refresh_timer"))
        self.assertEqual(tuple(card._detail_widgets), original_groups)
        self.assertTrue(card._expanded)
        purchases.refresh_purchase_batch_alchemy_ready_at(self.path)
        card._reload()
        self.assertEqual(next(iter(card._recipe_alchemy_labels.values())).text(), "可炼金")

    def test_expanding_recipe_updates_after_displayed_ready_hour(self):
        last = datetime(2030, 1, 1, 19, 12, tzinfo=TZ)
        self.receive_recipe([last] * 10)
        card = PurchaseBatchCard(self.path, purchases.load_purchase_batch(self.path), expanded=True)
        self.addCleanup(card.deleteLater)
        card.show()
        self.addCleanup(card.close)
        self.app.processEvents()
        with patch("ui.widgets.purchase_batch_card.purchase_batch_recipe_alchemy_status_text", side_effect=lambda entry: purchases.purchase_batch_recipe_alchemy_status_text(entry, now=last.replace(hour=20))):
            card._recipe_group_buttons[0].click()
        self.assertEqual(card.findChild(QLabel, "purchaseRecipeAlchemyTime").text(), "可炼金")

    def test_unknown_invalid_or_wrong_asset_cd_never_uses_receipt_date(self):
        last = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        self.receive_recipe([last] * 10)
        for value in (None, "invalid", float("nan"), float("inf"), -1, 1e100):
            self.inventory[-1]["cooldown_ends_at"] = value
            entry = purchases.load_purchase_batch(self.path)["recipes"][0]
            self.assertNotIn("alchemy_ready_at", entry)
            self.assertIn("待刷新 Steam 库存确认", purchases.purchase_batch_recipe_alchemy_status_text(entry))
        entry["materials"][-1]["steam_cooldown"] = {"assetid": "another", "ends_at": last.timestamp()}
        self.assertIn("待刷新", purchases.purchase_batch_recipe_alchemy_status_text(entry))

    def test_partial_receipt_and_legacy_group_date(self):
        last = datetime(2026, 10, 25, 22, 12, tzinfo=TZ)
        self.receive_recipe([last] * 10)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        entry = raw["recipes"][0]
        for row in entry["materials"]:
            row.pop("received_at", None)
        entry.pop("alchemy_time_source", None)
        entry["alchemy_ready_at"] = "2026-11-01T23:00:00+08:00"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        updated = purchases.load_purchase_batch(self.path)
        self.assertEqual(purchases.purchase_recipe_received_date(updated["recipes"][0], updated), "2026-10-25")
        purchases.set_purchase_batch_material_status(self.path, entry["id"], entry["materials"][0]["row_id"], STATUS_ORDERED)
        partial = purchases.load_purchase_batch(self.path)
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(partial["recipes"][0]), "")

    def test_exact_hour_is_preserved_and_partial_hour_rounds_up(self):
        self.assertEqual(purchases.compute_alchemy_ready_at(datetime(2026, 10, 26, 19, tzinfo=TZ)), datetime(2026, 10, 26, 19, tzinfo=TZ))
        self.assertEqual(purchases.compute_alchemy_ready_at(datetime(2026, 10, 26, 18, 42, tzinfo=TZ)), datetime(2026, 10, 26, 19, tzinfo=TZ))
        self.assertEqual(purchases.compute_alchemy_ready_at(datetime(2026, 12, 31, 23, 59, tzinfo=TZ)), datetime(2027, 1, 1, 0, tzinfo=TZ))

    def test_exact_hour_cd_displays_ready_at_that_hour(self):
        end = datetime(2030, 1, 1, 19, tzinfo=TZ)
        self.receive_recipe([end] * 10)
        entry = purchases.load_purchase_batch(self.path)["recipes"][0]
        self.assertEqual(datetime.fromisoformat(entry["alchemy_ready_at"]), end)
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(entry, now=end - timedelta(seconds=1)), "炼金时间:2030-1-1 19:00")
        self.assertEqual(purchases.purchase_batch_recipe_alchemy_status_text(entry, now=end), "可炼金")


if __name__ == "__main__":
    unittest.main()
