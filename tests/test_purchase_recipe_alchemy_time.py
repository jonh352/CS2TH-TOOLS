from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from core.purchase_batches import (
    add_recipe_to_purchase_batch,
    compute_alchemy_ready_at,
    create_purchase_batch,
    load_purchase_batch,
    purchase_batch_alchemy_status_text,
    purchase_batch_recipe_alchemy_status_text,
    refresh_purchase_batch_alchemy_ready_at,
    set_purchase_batch_material_status,
)
from core.purchase_tracking import STATUS_CANCELLED, STATUS_RECEIVED
from tests.test_purchase_batches import _special_recipe
from ui.widgets.purchase_batch_card import PurchaseBatchCard


class PurchaseRecipeAlchemyTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_recipes_record_independent_times_and_batch_waits_for_every_recipe(self):
        first_at = datetime(2026, 10, 20, 9, 34, tzinfo=timezone(timedelta(hours=8)))
        second_at = first_at + timedelta(days=1, hours=2)
        with TemporaryDirectory() as directory, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR", Path(directory)
        ):
            path = create_purchase_batch("独立时间", profile_id="p", steam_id="", account_name="p", inventory_items=[])
            first_id = add_recipe_to_purchase_batch(path, _special_recipe())
            second_id = add_recipe_to_purchase_batch(path, _special_recipe())
            batch = load_purchase_batch(path)
            first_rows = batch["recipes"][0]["materials"]
            second_rows = batch["recipes"][1]["materials"]
            with patch("core.purchase_batches._utc_now", return_value=first_at.isoformat()):
                for row in first_rows[:-1]:
                    set_purchase_batch_material_status(path, first_id, row["row_id"], STATUS_RECEIVED)
                self.assertNotIn("alchemy_ready_at", load_purchase_batch(path)["recipes"][0])
                set_purchase_batch_material_status(path, first_id, first_rows[-1]["row_id"], STATUS_RECEIVED)
            self.assertIsNone(refresh_purchase_batch_alchemy_ready_at(path, verified_at=first_at))
            partial = load_purchase_batch(path)
            first_ready = compute_alchemy_ready_at(first_at).isoformat()
            self.assertEqual(partial["recipes"][0]["alchemy_ready_at"], first_ready)
            self.assertNotIn("alchemy_ready_at", partial["recipes"][1])
            self.assertNotIn("alchemy_ready_at", partial)
            self.assertEqual(purchase_batch_alchemy_status_text(partial, now=first_at), "")
            self.assertEqual(
                purchase_batch_recipe_alchemy_status_text(partial["recipes"][0], now=first_at),
                "炼金时间:2026-10-27 10:00",
            )
            with patch(
                "ui.widgets.purchase_batch_card.purchase_batch_recipe_alchemy_status_text",
                side_effect=lambda entry: purchase_batch_recipe_alchemy_status_text(entry, now=first_at),
            ):
                card = PurchaseBatchCard(path, partial)
                card._set_expanded(True)
            labels = card.findChildren(QLabel, "purchaseRecipeAlchemyTime")
            self.assertEqual(len(labels), 1)
            self.assertTrue(labels[0].text().startswith("炼金时间:"))
            with patch("core.purchase_batches._utc_now", return_value=second_at.isoformat()):
                for row in second_rows:
                    set_purchase_batch_material_status(path, second_id, row["row_id"], STATUS_RECEIVED)
            batch_ready = refresh_purchase_batch_alchemy_ready_at(path, verified_at=second_at)
            complete = load_purchase_batch(path)
            self.assertEqual(complete["recipes"][0]["alchemy_ready_at"], first_ready)
            self.assertEqual(complete["recipes"][1]["alchemy_ready_at"], compute_alchemy_ready_at(second_at).isoformat())
            self.assertEqual(batch_ready, compute_alchemy_ready_at(second_at).isoformat())
            self.assertEqual(refresh_purchase_batch_alchemy_ready_at(path, verified_at=second_at + timedelta(days=1)), batch_ready)
            self.assertEqual(load_purchase_batch(path)["recipes"][0]["alchemy_ready_at"], first_ready)

    def test_backfill_uses_last_receipt_and_departure_review_keeps_original_time(self):
        first = datetime(2026, 10, 10, 9, 12, tzinfo=timezone(timedelta(hours=8)))
        last = first + timedelta(days=1, hours=4)
        entry = {"materials": [
            {"status": STATUS_RECEIVED, "received_at": first.isoformat()},
            {"status": STATUS_RECEIVED, "received_at": last.isoformat()},
        ]}
        self.assertEqual(
            purchase_batch_recipe_alchemy_status_text(entry, now=last),
            "炼金时间:2026-10-18 14:00",
        )
        self.assertNotIn("alchemy_ready_at", entry)
        entry["alchemy_ready_at"] = compute_alchemy_ready_at(last).isoformat()
        self.assertEqual(
            purchase_batch_recipe_alchemy_status_text(entry, now=last + timedelta(days=8)), "可炼金"
        )
        entry["materials"][0]["inventory_missing_since"] = last.isoformat()
        self.assertEqual(purchase_batch_recipe_alchemy_status_text(entry, now=last), "")
        self.assertEqual(entry["alchemy_ready_at"], compute_alchemy_ready_at(last).isoformat())

    def test_repurchase_clears_only_the_affected_recipe_clock(self):
        when = datetime(2026, 10, 20, 9, 34, tzinfo=timezone(timedelta(hours=8)))
        with TemporaryDirectory() as directory, patch(
            "core.purchase_batches.PURCHASE_BATCHES_DIR", Path(directory)
        ):
            path = create_purchase_batch("补买", profile_id="p", steam_id="", account_name="p", inventory_items=[])
            entry_ids = [add_recipe_to_purchase_batch(path, _special_recipe(1)) for _ in range(2)]
            entries = load_purchase_batch(path)["recipes"]
            with patch("core.purchase_batches._utc_now", return_value=when.isoformat()):
                for entry in entries:
                    set_purchase_batch_material_status(path, entry["id"], entry["materials"][0]["row_id"], STATUS_RECEIVED)
            refresh_purchase_batch_alchemy_ready_at(path, verified_at=when)
            old_ready = load_purchase_batch(path)["recipes"][1]["alchemy_ready_at"]
            row_id = entries[0]["materials"][0]["row_id"]
            set_purchase_batch_material_status(path, entry_ids[0], row_id, STATUS_CANCELLED)
            partial = load_purchase_batch(path)
            self.assertNotIn("alchemy_ready_at", partial)
            self.assertNotIn("alchemy_ready_at", partial["recipes"][0])
            self.assertEqual(partial["recipes"][1]["alchemy_ready_at"], old_ready)
            later = when + timedelta(days=2)
            with patch("core.purchase_batches._utc_now", return_value=later.isoformat()):
                set_purchase_batch_material_status(path, entry_ids[0], row_id, STATUS_RECEIVED)
            self.assertEqual(
                load_purchase_batch(path)["recipes"][0]["alchemy_ready_at"],
                compute_alchemy_ready_at(later).isoformat(),
            )
