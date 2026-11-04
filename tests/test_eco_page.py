import copy
import os
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from core import purchase_batches as purchases
from core.eco_furnace import EcoCredentials, purchase_recipe_eco_readiness
from core.eco_publication import record_recipe_publication, recipe_publication, sync_furnace_publications, furnace_is_published
from core.purchase_tracking import STATUS_RECEIVED
from tests.test_purchase_batches import _special_recipe
from ui.pages.purchase_manage import PurchaseManagePage
from ui.widgets.eco_recipe_card import EcoRecipeCard
from ui.widgets.recipe_result_group import RecipeResultGroup
from ui.dialogs.eco_furnace_dialog import EcoFurnaceDialog
from ui.dialogs.eco_furnace_help_dialog import EcoFurnaceHelpDialog


class EcoPageTests(unittest.TestCase):
    def test_platform_confirmation_syncs_exact_furnace_without_losing_prices(self):
        path, batch, entry = self.add_recipe("Ten")
        payload = {"ReceiveSteamID": batch["steam_id"], "AssetList": [{"AssetId": row["matched_assetid"]} for row in entry["materials"]], "_material_listing": {"prices": {"a": 12.3}, "listed_assets": [], "uncertain": False}}
        record_recipe_publication(path, entry["id"], "submitted", payload=payload, furnace_id="bound")
        row = {"FurnaceID": "bound", "HL_FurnaceState": 100, "CanPayment": True, "IsMatching": False, "IsFurnaceUser": True}
        self.assertEqual(sync_furnace_publications([row]), 0)
        row["CanPayment"] = False
        self.assertEqual(sync_furnace_publications([row]), 0)
        row["IsMatching"] = True
        self.assertEqual(sync_furnace_publications([dict(row, FurnaceID="another")]), 0)
        self.assertEqual(sync_furnace_publications([row]), 1)
        record_recipe_publication(path, entry["id"], "submitted", payload=payload, furnace_id="bound")
        saved = purchases.load_purchase_batch(path)["recipes"][0]["eco_publication"]
        self.assertEqual(saved["state"], "listed")
        self.assertEqual(saved["source"], "eco_confirmed")
        self.assertEqual(saved["material_listing"], payload["_material_listing"])
        self.assertEqual(sync_furnace_publications([]), 0)
        for state in (401, 402):
            self.assertFalse(furnace_is_published(dict(row, HL_FurnaceState=state)))

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch.object(purchases, "PURCHASE_BATCHES_DIR", Path(self.temp)))
        self.inventories = {}
        self.stack.enter_context(patch("ui.pages.purchase_manage.load_profile_inventory_items", side_effect=lambda profile: self.inventories.get(profile, [])))
        self.stack.enter_context(patch("ui.pages.purchase_manage.list_profile_entries", return_value=[]))
        self.stack.enter_context(patch("ui.pages.purchase_manage.repair_purchase_account_links"))
        self.counter = 10000

    def add_recipe(self, profile="Ten", *, state="ready"):
        steam = "76561198000000001" if profile == "Ten" else "76561198000000002"
        path = purchases.create_purchase_batch("来源批次-"+profile, profile_id=profile, steam_id=steam, account_name=profile, inventory_items=[])
        entry_id = purchases.add_recipe_to_purchase_batch(path, _special_recipe(), title="配方-"+profile)
        batch = purchases.load_purchase_batch(path)
        entry = batch["recipes"][0]
        for row in entry["materials"]:
            self.counter += 1
            row["status"] = STATUS_RECEIVED
            row["received_at"] = "2026-10-01T02:00:00+00:00"
            row["matched_assetid"] = str(self.counter)
            source = entry["recipe"]["substrates_display"][row["substrate_index"]]
            self.inventories.setdefault(profile, []).append({
                "assetid": str(self.counter), "market_name": source["name"], "float": source["float_value"],
                "marketable": 1, "steam_inventory_context_id": 2,
            })
        if state == "pending":
            entry["materials"][0]["status"] = "pending"
        elif state == "completed":
            entry["tradeup_completed"] = True
        elif state == "missing":
            self.inventories[profile].pop()
        elif state == "cooling":
            self.inventories[profile][-1].update(cooldown_kind="trade_hold", cooldown_ends_at=time.time()+86400, marketable=0)
        elif state == "untradable":
            self.inventories[profile][-1].update(marketable=0, tradable=False)
        elif state == "wrong_wear":
            self.inventories[profile][-1]["float"] += 0.01
        purchases._write_batch(path, batch)
        return path, batch, entry

    def make_page(self):
        page = PurchaseManagePage()
        self.addCleanup(lambda: (page.close(), page.deleteLater(), self.app.processEvents()))
        page._show_section("eco")
        return page

    def cards(self, page):
        return [page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), EcoRecipeCard)]

    def test_one_page_lists_accounts_and_excludes_ineligible_recipes(self):
        self.add_recipe("Ten")
        self.add_recipe("Nine")
        for state in ("pending", "completed", "missing", "cooling", "untradable", "wrong_wear"):
            self.add_recipe(state=state)
        page = self.make_page()
        self.assertEqual(list(page._section_buttons)[-2:], ["history", "eco"])
        self.assertEqual(len(self.cards(page)), 2)
        text = " ".join(label.text() for card in self.cards(page) for label in card.findChildren(QLabel))
        for value in ("Ten", "Nine"):
            self.assertIn(value, text)
        for value in ("来源批次", "采购批次", "76561198000000001", "76561198000000002"):
            self.assertNotIn(value, text)
        heading = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), QPushButton))
        self.assertIsNotNone(heading)
        self.assertTrue(heading.text().endswith("· 2 炉"))
        heading.click()
        self.assertTrue(all(card.isHidden() for card in self.cards(page)))
        page._rebuild_purchase_batch_rows()
        self.assertTrue(all(card.isHidden() for card in self.cards(page)))
        heading = next(page._purchase_batch_layout.itemAt(i).widget() for i in range(page._purchase_batch_layout.count()) if isinstance(page._purchase_batch_layout.itemAt(i).widget(), QPushButton))
        heading.click()
        self.assertTrue(all(not card.isHidden() for card in self.cards(page)))
        self.assertIn("2个可合炉配方", page._title_count_label.text())
        card = self.cards(page)[0]
        card.material_button.click()
        self.assertEqual(card._table.rowCount(), 10)
        self.assertNotEqual(card._table.item(0, 6).text(), "")

    def test_expired_cd_with_stale_unsellable_flag_still_shows_in_eco(self):
        path, batch, entry = self.add_recipe("Ten")
        for item in self.inventories["Ten"]:
            item.update(marketable=0, tradable=False, steam_inventory_context_id=16,
                        cooldown_kind="trade_hold", cooldown_ends_at=time.time()-60)
        self.assertTrue(purchase_recipe_eco_readiness(batch, entry, self.inventories["Ten"])[0])
        page = self.make_page()
        self.assertEqual(len(self.cards(page)), 1)

    def test_unknown_cooldown_does_not_pass_with_stale_flags(self):
        path, batch, entry = self.add_recipe("Ten")
        self.inventories["Ten"][-1].update(marketable=0, steam_inventory_context_id=16,
                                           cooldown_kind="trade_hold", cooldown_ends_at=None)
        self.assertFalse(purchase_recipe_eco_readiness(batch, entry, self.inventories["Ten"])[0])
        page = self.make_page()
        self.assertEqual(len(self.cards(page)), 0)

    def test_account_and_item_filters(self):
        self.add_recipe("Ten")
        self.add_recipe("Nine")
        page = self.make_page()
        page._account_combo.setCurrentIndex(page._account_combo.findData("Ten"))
        self.assertEqual(len(self.cards(page)), 1)
        page._item_search_edit.setText("不存在的饰品")
        page._item_search_timer.stop()
        page._rebuild_purchase_batch_rows()
        self.assertEqual(len(self.cards(page)), 0)
        page._item_search_edit.clear()
        page._item_search_timer.stop()
        page._rebuild_purchase_batch_rows()
        self.assertEqual(len(self.cards(page)), 1)

    def test_publish_button_passes_correct_bound_account_and_assets(self):
        path, batch, entry = self.add_recipe("Ten")
        page = self.make_page()
        with patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceDialog") as dialog:
            self.cards(page)[0].publish_button.click()
        dialog.assert_called_once()
        self.assertEqual(dialog.call_args.kwargs["profile_id"], "Ten")
        recipe = dialog.call_args.args[0]
        self.assertEqual({row["steam_id"] for row in recipe["substrates_display"]}, {batch["steam_id"]})
        self.assertEqual({row["steam_assetid"] for row in recipe["substrates_display"]}, {row["matched_assetid"] for row in entry["materials"]})

    def test_settings_and_guide_exist_even_when_no_eligible_recipes(self):
        page = self.make_page()
        self.assertEqual(len(self.cards(page)), 0)
        self.assertTrue(page._eco_settings_btn.isVisibleTo(page))
        self.assertTrue(page._eco_help_btn.isVisibleTo(page))
        with patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceDialog") as dialog:
            page._eco_settings_btn.click()
            self.assertEqual(dialog.call_args.kwargs["initial_tab"], "settings")
        with patch("ui.dialogs.eco_furnace_help_dialog.EcoFurnaceHelpDialog") as guide:
            page._eco_help_btn.click()
            guide.return_value.exec.assert_called_once()
        page._show_section("purchasing")
        self.assertTrue(page._eco_settings_btn.isHidden())
        self.assertTrue(page._eco_help_btn.isHidden())

    def test_settings_dialog_has_no_publish_tab_without_recipe(self):
        with patch("ui.dialogs.eco_furnace_dialog.load_eco_credentials", return_value=EcoCredentials("", "")), patch("ui.dialogs.eco_furnace_dialog.list_profile_entries", return_value=[]):
            dialog = EcoFurnaceDialog({}, initial_tab="settings")
        self.assertIs(dialog.tabs.currentWidget(), dialog._settings_tab)
        self.assertFalse(dialog.tabs.isTabVisible(0))
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()

    def test_marked_recipe_moves_to_listed_filter_and_survives_reopen(self):
        path, batch, entry = self.add_recipe()
        page = self.make_page()
        card = self.cards(page)[0]
        card.mark_publication_button.click()
        self.assertEqual(self.cards(page), [])
        page._eco_status_buttons["listed"].click()
        listed = self.cards(page)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].publish_button.text(), "查看合炉")
        with patch.object(page, "_open_eco_furnaces") as manage:
            # Signal was connected before patching; use the card signal itself to
            # verify this is a management action, never another create action.
            emitted = []
            listed[0].publish_requested.connect(lambda *args: emitted.append(args))
            listed[0].manage_requested.disconnect()
            listed[0].manage_requested.connect(manage)
            listed[0].publish_button.click()
            manage.assert_called_once()
            self.assertEqual(emitted, [])
        loaded = purchases.load_purchase_batch(path)
        self.assertEqual(recipe_publication(loaded, loaded["recipes"][0])["state"], "listed")
        page._eco_status_buttons["all"].click()
        self.cards(page)[0].mark_publication_button.click()
        page._eco_status_buttons["unlisted"].click()
        self.assertEqual(len(self.cards(page)), 1)

    def test_submitted_recipe_remains_visible_after_materials_leave_inventory(self):
        path, batch, entry = self.add_recipe()
        payload = {"ReceiveSteamID": batch["steam_id"], "AssetList": [{"AssetId": row["matched_assetid"]} for row in entry["materials"]]}
        record_recipe_publication(path, entry["id"], "submitted", payload=payload)
        self.inventories["Ten"] = []
        page = self.make_page()
        self.assertEqual(len(self.cards(page)), 0)
        page._eco_status_buttons["listed"].click()
        self.assertEqual(len(self.cards(page)), 1)
        text = self.cards(page)[0].findChild(QLabel, "ecoPublicationStatus").text()
        self.assertIn("待平台确认", text)

    def test_publication_identity_does_not_follow_replaced_assets_or_accounts(self):
        path, batch, entry = self.add_recipe()
        record_recipe_publication(path, entry["id"], "listed")
        loaded = purchases.load_purchase_batch(path)
        self.assertTrue(recipe_publication(loaded, loaded["recipes"][0]))
        replaced = copy.deepcopy(loaded)
        replaced["recipes"][0]["materials"][0]["matched_assetid"] = "999999"
        self.assertEqual(recipe_publication(replaced, replaced["recipes"][0]), {})
        loaded["steam_id"] = "76561198000000002"
        self.assertEqual(recipe_publication(loaded, loaded["recipes"][0]), {})

    def test_wrong_create_payload_cannot_mark_recipe_as_submitted(self):
        path, batch, entry = self.add_recipe()
        with self.assertRaisesRegex(ValueError, "账号或材料"):
            record_recipe_publication(path, entry["id"], "submitted", payload={"ReceiveSteamID": batch["steam_id"], "AssetList": []})
        loaded = purchases.load_purchase_batch(path)
        self.assertEqual(recipe_publication(loaded, loaded["recipes"][0]), {})

    def test_guide_four_pages_have_images_and_navigation(self):
        dialog = EcoFurnaceHelpDialog()
        for page in range(4):
            dialog._show_page(page)
            self.assertFalse(dialog.image_label.pixmap().isNull())
            self.assertEqual(dialog.page_label.text(), f"{page+1} / 4")
            self.assertTrue(dialog.description.text())
        self.assertTrue(dialog.next_button.isHidden())
        self.assertFalse(dialog.done_button.isHidden())
        dialog.previous_button.click()
        self.assertEqual(dialog.page_index, 2)
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()

    def test_old_result_group_has_no_publish_entry(self):
        group = RecipeResultGroup(1, _special_recipe(), enable_save=True)
        self.assertFalse(hasattr(group, "eco_publish_btn"))
        group.close()
        group.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
