import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from core import purchase_batches as purchases
from core.purchase_tracking import STATUS_RECEIVED, STATUS_PENDING, STATUS_ORDERED
from tests.test_purchase_batches import _special_recipe
from ui.pages.purchase_manage import PurchaseManagePage
from ui.dialogs.edit_purchase_recipe_dialog import EditPurchaseRecipeDialog
from ui.widgets.purchase_batch_card import PurchaseBatchCard


class PurchaseRecipeFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.dir_patch = patch.object(purchases, "PURCHASE_BATCHES_DIR", Path(self.temp.name))
        self.dir_patch.start()
        self.time_patch = patch.object(purchases, "_utc_now", return_value="2026-10-25T02:13:00+00:00")
        self.time_patch.start()

    def tearDown(self):
        self.app.processEvents()
        self.time_patch.stop()
        self.dir_patch.stop()
        self.temp.cleanup()

    def batch(self, profile, name="批次"):
        return purchases.create_purchase_batch(name, profile_id=profile, steam_id="steam-"+profile, account_name=profile, inventory_items=[])

    def recipe(self, path, received=False):
        entry_id = purchases.add_recipe_to_purchase_batch(path, _special_recipe())
        if received:
            batch = purchases.load_purchase_batch(path)
            entry = next(row for row in batch["recipes"] if row["id"] == entry_id)
            for row in entry["materials"]:
                row["status"] = STATUS_RECEIVED
                row["received_at"] = "2026-10-25T02:13:00+00:00"
                row["matched_assetid"] = entry_id + row["row_id"]
                row["steam_cooldown"] = {"assetid": row["matched_assetid"], "ends_at": datetime(2030, 1, 1, 19, 12, tzinfo=timezone(timedelta(hours=8))).timestamp(), "clear": False}
            purchases._write_batch(path, batch)
        return entry_id

    def test_daily_groups_merge_accounts_and_archive_by_completion_day(self):
        for profile in ("Ten", "Nine", "Six"):
            path = self.batch(profile)
            self.recipe(path)
            self.recipe(path)
        page = PurchaseManagePage()
        headings = page.findChildren(QPushButton, "purchaseDayHeading")
        self.assertEqual([label.text() for label in headings], ["2026-10-25 · 6 炉"])
        cards = [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count())]
        cards = [card for card in cards if isinstance(card, PurchaseBatchCard)]
        self.assertEqual(len(cards), 6)
        self.assertEqual({card._payload["account_name"] for card in cards}, {"Ten", "Nine", "Six"})
        self.assertTrue(all(len(card._payload["recipes"]) == 1 for card in cards))
        card = cards[0]
        with patch.object(purchases, "_utc_now", return_value="2026-10-27T02:13:00+00:00"):
            purchases.set_purchase_batch_recipe_tradeup_completed(card._path, card._payload["recipes"][0]["id"], True)
        page._show_section("tradeup_completed")
        headings = [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count())]
        self.assertEqual([widget.text() for widget in headings if isinstance(widget, QPushButton)], ["2026-10-27 · 1 炉"])
        page.close()
        page.deleteLater()

    def test_recipe_bulk_mark_does_not_touch_other_recipes(self):
        path = self.batch("Ten")
        first, second = self.recipe(path), self.recipe(path)
        purchases.toggle_all_purchase_batch_materials_ordered(path, {first})
        entries = {entry["id"]: entry for entry in purchases.load_purchase_batch(path)["recipes"]}
        self.assertTrue(all(row["status"] == STATUS_ORDERED for row in entries[first]["materials"]))
        self.assertTrue(all(row["status"] == STATUS_PENDING for row in entries[second]["materials"]))

    def test_all_ten_received_materials_departure_auto_archives_only_complete_recipe(self):
        path = self.batch("Ten")
        gone = self.recipe(path, received=True)
        partial = self.recipe(path, received=True)
        incomplete = self.recipe(path, received=True)
        batch = purchases.load_purchase_batch(path)
        batch["recipes"][2]["materials"][0]["status"] = STATUS_ORDERED
        present = batch["recipes"][1]["materials"][0]["matched_assetid"]
        purchases._write_batch(path, batch)
        inventory = [{"assetid": present, "marketable": True}]
        purchases.reconcile_purchase_batches_for_profile("Ten", inventory)
        rows = {entry["id"]: entry for entry in purchases.load_purchase_batch(path)["recipes"]}
        self.assertTrue(rows[gone]["tradeup_completed"])
        self.assertEqual(rows[gone]["archive_reason"], "all_materials_departed")
        self.assertFalse(any(row.get("inventory_missing_since") for row in rows[gone]["materials"]))
        for entry_id in (partial, incomplete):
            self.assertFalse(rows[entry_id].get("tradeup_completed"))
        archived_at = rows[gone]["tradeup_completed_at"]
        purchases.reconcile_purchase_batches_for_profile("Ten", inventory)
        self.assertEqual(purchases.load_purchase_batch(path)["recipes"][0]["tradeup_completed_at"], archived_at)
        self.assertEqual(purchases.purchase_recipe_section(rows[gone]), "tradeup_completed")

    def test_collapsed_recipe_builds_details_only_on_first_open(self):
        path = self.batch("Ten")
        self.recipe(path)
        self.recipe(path)
        page = PurchaseManagePage()
        cards = [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count())]
        cards = [card for card in cards if isinstance(card, PurchaseBatchCard)]
        self.assertTrue(all(not card._tables and not card._product_tables for card in cards))
        card = cards[0]
        card._recipe_group_buttons[0].click()
        table = card._table
        self.assertIsNotNone(table)
        self.assertEqual(table.rowCount(), 10)
        card._recipe_group_buttons[0].click()
        card._recipe_group_buttons[0].click()
        self.assertIs(card._table, table)
        self.assertFalse(cards[1]._tables)
        page.close()
        page.deleteLater()

    def test_saved_empty_material_filter_keeps_recipe_header_and_reset_control(self):
        path = self.batch("Ten")
        self.recipe(path, received=True)
        page = PurchaseManagePage()
        card = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), PurchaseBatchCard))
        card._filter.setCurrentText("待购买")
        self.assertEqual(len(card._recipe_group_buttons), 1)
        page._rebuild_purchase_batch_rows()
        card = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), PurchaseBatchCard))
        self.assertEqual(card._filter.currentText(), "全部材料")
        self.assertEqual(len(card._recipe_group_buttons), 1)
        self.assertEqual(len(card._recipe_groups()), 1)
        self.assertFalse(card._recipe_tools.isHidden())
        card._filter.setCurrentText("全部材料")
        self.assertEqual(card._table.rowCount(), 10)
        page.close()
        page.deleteLater()

    def test_daily_purchase_reuses_account_day_and_keeps_inventory_baseline(self):
        args = dict(profile_id="Ten", steam_id="steam-Ten", account_name="Ten", inventory_items=[])
        first = purchases.daily_purchase_batch(**args)
        self.recipe(first)
        self.assertEqual(purchases.daily_purchase_batch(**args), first)
        other = purchases.daily_purchase_batch(**dict(args, profile_id="Nine"))
        self.assertNotEqual(other, first)
        self.assertEqual(purchases.load_purchase_batch(first)["name"], "2026-10-25")

    def test_date_defaults_closed_and_day_actions_preserve_other_records(self):
        first, second = self.batch("Ten"), self.batch("Nine")
        self.recipe(first)
        self.recipe(second)
        archived = self.recipe(first)
        purchases.set_purchase_batch_recipe_tradeup_completed(first, archived, True)
        with patch.object(purchases, "_utc_now", return_value="2026-10-26T02:13:00+00:00"):
            other_day = self.recipe(first)
        page = PurchaseManagePage()
        cards = [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count())]
        self.assertTrue(all(card.isHidden() for card in cards if isinstance(card, PurchaseBatchCard)))
        def day_bar():
            for i in range(page._purchase_batch_layout.count()):
                widget = page._purchase_batch_layout.itemAt(i).widget()
                heading = widget.findChild(QPushButton, "purchaseDayHeading") if widget else None
                if heading and heading.text().startswith("2026-10-25"):
                    return widget
        with patch("ui.pages.purchase_manage.reconcile_all_purchase_records_for_profile") as reconcile:
            day_bar().findChild(QPushButton, "purchaseDayReconcileBtn").click()
            self.assertEqual({call.args[0] for call in reconcile.call_args_list}, {"Ten", "Nine"})
            self.assertEqual(reconcile.call_count, 2)
        with patch("ui.pages.purchase_manage.ask_confirmation", return_value=True):
            day_bar().findChild(QPushButton, "alchemyClearFileBtn").click()
        self.assertEqual({entry["id"] for entry in purchases.load_purchase_batch(first)["recipes"]}, {archived, other_day})
        self.assertEqual(purchases.load_purchase_batch(second)["recipes"], [])
        page._show_section("tradeup_completed")
        cards = [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count())]
        self.assertTrue(all(card.isHidden() for card in cards if isinstance(card, PurchaseBatchCard)))
        page.close()
        page.deleteLater()

    def test_merged_batch_button_opens_simulation_for_received_recipe(self):
        path = self.batch("Ten")
        entry_id = self.recipe(path, received=True)
        page = PurchaseManagePage()
        emitted = []
        page.simulate_tradeup_requested.connect(emitted.append)
        page._show_section("purchase_completed")
        card = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), PurchaseBatchCard))
        card._set_expanded(True)
        button = card._recipe_one_click_buttons[0]
        self.assertTrue(button.isEnabled())
        button.click()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["recipe_entry_id"], entry_id)
        self.assertEqual(emitted[0]["profile_id"], "Ten")
        self.assertEqual(len(emitted[0]["asset_ids"]), 10)
        # 进入模拟不应执行汰换或改变采购状态。
        self.assertFalse(purchases.load_purchase_batch(path)["recipes"][0].get("tradeup_completed"))
        page.close()
        page.deleteLater()

    def test_merged_view_preserves_material_filter_after_receipt(self):
        path = self.batch("Ten")
        self.recipe(path)
        page = PurchaseManagePage()
        card = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), PurchaseBatchCard))
        card._filter.setCurrentText("待购买")
        batch = purchases.load_purchase_batch(path)
        for row in batch["recipes"][0]["materials"]:
            row["status"] = STATUS_RECEIVED
            row["received_at"] = "2026-10-25T02:13:00+00:00"
        purchases._write_batch(path, batch)
        page._show_section("purchase_completed")
        card = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), PurchaseBatchCard))
        card._set_expanded(True)
        self.assertEqual(card._filter.currentText(), "待购买")
        card._filter.setCurrentText("全部材料")
        self.assertEqual(len(card._recipe_groups()), 1)
        page.close()
        page.deleteLater()

    def test_move_within_same_account_preserves_receipt_and_clock(self):
        source, target = self.batch("Ten"), self.batch("Ten", "目标批次")
        entry_id = self.recipe(source, received=True)
        self.recipe(source)
        before = purchases.load_purchase_batch(source)["recipes"][0]
        self.assertEqual(purchases.move_purchase_batch_recipe(source, entry_id, target), 0)
        self.assertEqual(purchases.load_purchase_batch(target)["recipes"][0], before)
        self.assertEqual(len(purchases.load_purchase_batch(source)["recipes"]), 1)

    def test_cross_account_move_resets_only_moved_recipe_inventory_matches(self):
        source, target = self.batch("Ten"), self.batch("Nine")
        entry_id = self.recipe(source, received=True)
        self.recipe(source, received=True)
        other = copy.deepcopy(purchases.load_purchase_batch(source)["recipes"][1])
        moved_count = purchases.move_purchase_batch_recipe(source, entry_id, target)
        moved = purchases.load_purchase_batch(target)["recipes"][0]
        self.assertEqual(moved_count, len(moved["materials"]))
        self.assertNotIn("alchemy_ready_at", moved)
        self.assertEqual({row["status"] for row in moved["materials"]}, {STATUS_ORDERED})
        self.assertTrue(all("matched_assetid" not in row and "received_at" not in row for row in moved["materials"]))
        self.assertEqual(purchases.load_purchase_batch(source)["recipes"], [other])

    def test_failed_move_rolls_back_without_duplication_or_loss(self):
        source, target = self.batch("Ten"), self.batch("Nine")
        entry_id = self.recipe(source)
        before_source = purchases.load_purchase_batch(source)
        before_target = purchases.load_purchase_batch(target)
        write = purchases._write_batch
        failed = False
        def fail_source_once(path, payload):
            nonlocal failed
            if path.resolve() == source.resolve() and not failed:
                failed = True
                raise OSError("模拟保存失败")
            write(path, payload)
        with patch.object(purchases, "_write_batch", side_effect=fail_source_once):
            with self.assertRaises(OSError):
                purchases.move_purchase_batch_recipe(source, entry_id, target)
        self.assertEqual(purchases.load_purchase_batch(source), before_source)
        self.assertEqual(purchases.load_purchase_batch(target), before_target)

    def test_single_delete_and_cleanup_preserve_other_recipes_in_mixed_batch(self):
        path = self.batch("Ten")
        completed = self.recipe(path, received=True)
        pending = self.recipe(path)
        to_delete = self.recipe(path)
        purchases.delete_purchase_batch_recipe(path, to_delete)
        purchases.set_purchase_batch_recipe_tradeup_completed(path, completed, True)
        with patch.object(purchases, "_tradeup_history_path", return_value=Path(self.temp.name)/"history.json"):
            self.assertEqual(purchases.delete_tradeup_completed_batches(), 1)
        self.assertEqual([row["id"] for row in purchases.load_purchase_batch(path)["recipes"]], [pending])

    def test_ready_recipes_include_received_entry_in_partially_received_batch(self):
        path = self.batch("Ten")
        received = self.recipe(path, received=True)
        self.recipe(path)
        with patch.object(purchases, "load_profile_inventory_items", return_value=[]), patch.object(
            purchases, "purchase_batch_recipe_live_readiness", return_value=(True, "ready")
        ):
            result = purchases.list_ready_purchase_batch_recipes("Ten")
        self.assertEqual(result[0][2], [received])

    def test_receipt_date_uses_last_material_and_beijing_day(self):
        entry = {"materials": [{"received_at": "2026-10-24T15:59:00+00:00"}, {"received_at": "2026-10-24T16:01:00+00:00"}]}
        self.assertEqual(purchases.purchase_recipe_received_date(entry, {}), "2026-10-25")

    def test_batch_purchase_actions_leave_archived_recipe_materials_untouched(self):
        path = self.batch("Ten")
        completed = self.recipe(path)
        self.recipe(path)
        purchases.set_purchase_batch_recipe_tradeup_completed(path, completed, True)
        archived = copy.deepcopy(purchases.load_purchase_batch(path)["recipes"][0])
        purchases.mark_all_purchase_batch_materials_ordered(path)
        purchases.toggle_all_purchase_batch_materials_ordered(path)
        self.assertEqual(purchases.load_purchase_batch(path)["recipes"][0], archived)

    def test_editor_selects_account_without_batch_picker(self):
        source, target = self.batch("Ten"), self.batch("Nine")
        entry_id = self.recipe(source)
        with patch("ui.dialogs.edit_purchase_recipe_dialog.list_profile_entries", return_value=[]):
            dialog = EditPurchaseRecipeDialog(source, purchases.load_purchase_batch(source), purchases.load_purchase_batch(source)["recipes"][0], purchases.list_purchase_batches())
        dialog.account_combo.setCurrentIndex(dialog.account_combo.findData("Nine"))
        self.assertEqual(dialog.account_combo.currentData(), "Nine")
        self.assertFalse(hasattr(dialog, "batch_combo"))
        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
