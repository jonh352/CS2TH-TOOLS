import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import purchase_batches as purchases
from tests.test_purchase_batches import _special_recipe, _inventory


class ArchivedMaterialReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.directory = patch.object(purchases, "PURCHASE_BATCHES_DIR", Path(self.temp.name))
        self.directory.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.directory.stop)

    def make_entry(self):
        path = purchases.create_purchase_batch("测试批次", profile_id="Ten", steam_id="", account_name="Ten", inventory_items=[])
        entry_id = purchases.add_recipe_to_purchase_batch(path, _special_recipe(1))
        purchases.mark_all_purchase_batch_materials_ordered(path)
        return path, entry_id

    def test_manual_archive_releases_existing_material_for_active_recipe(self):
        old, old_id = self.make_entry()
        purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("shared")])
        purchases.set_purchase_batch_recipe_tradeup_completed(old, old_id, True)
        original = purchases.load_purchase_batch(old)
        new, new_id = self.make_entry()
        result = purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("shared")])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(purchases.load_purchase_batch(new)["recipes"][0]["materials"][0]["matched_assetid"], "shared")
        self.assertEqual(purchases.load_purchase_batch(old), original)
        with self.assertRaisesRegex(ValueError, "已被其他未汰换配方占用"):
            purchases.set_purchase_batch_recipe_tradeup_completed(old, old_id, False)
        self.assertTrue(purchases.load_purchase_batch(old)["recipes"][0]["tradeup_completed"])

    def test_archived_pending_rows_do_not_receive_materials(self):
        old, old_id = self.make_entry()
        purchases.set_purchase_batch_recipe_tradeup_completed(old, old_id, True)
        new, new_id = self.make_entry()
        result = purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("shared")])
        self.assertEqual(result["matched"], 1)
        self.assertNotIn("matched_assetid", purchases.load_purchase_batch(old)["recipes"][0]["materials"][0])
        self.assertEqual(purchases.load_purchase_batch(new)["recipes"][0]["materials"][0]["matched_assetid"], "shared")

    def test_actual_executed_recipe_still_blocks_stale_inventory_reuse(self):
        old, old_id = self.make_entry()
        purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("consumed")])
        purchases.set_purchase_batch_recipe_tradeup_completed(old, old_id, True)
        batch = purchases.load_purchase_batch(old)
        batch["recipes"][0]["tradeup_execution"] = {"completed_at": "2026-10-25T00:00:00+00:00"}
        purchases._write_batch(old, batch)
        new, new_id = self.make_entry()
        result = purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("consumed")])
        self.assertEqual(result["matched"], 0)
        self.assertNotIn("matched_assetid", purchases.load_purchase_batch(new)["recipes"][0]["materials"][0])

    def test_active_recipe_continues_to_reserve_material(self):
        old, old_id = self.make_entry()
        purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("shared")])
        new, new_id = self.make_entry()
        self.assertEqual(purchases.reconcile_purchase_batches_for_profile("Ten", [_inventory("shared")])["matched"], 0)


if __name__ == "__main__":
    unittest.main()
