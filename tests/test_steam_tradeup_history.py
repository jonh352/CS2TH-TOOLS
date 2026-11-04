from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from core import purchase_batches as purchases
from core.steam_inventory_history import history_event_datetime, parse_steam_inventory_history_page
from core.steam_tradeup_history import list_steam_tradeup_records, _snapshot, _wear_reference_price
from tests.test_purchase_batches import _special_recipe
from ui.pages.purchase_manage import PurchaseManagePage
from ui.workers.steam_history import SteamHistorySyncWorker


def event(**kwargs):
    return {
        "event_type": "tradeup", "completed_at": "2026-10-26T18:27:00+08:00",
        "inputs": [{"appid": 730, "classid": "11", "market_name": "Material"} for _ in range(10)],
        "outputs": [{"appid": 730, "classid": "22", "market_name": "Actual product"}],
        **kwargs,
    }


class SteamTradeupHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for target, kwargs in (
            ("PURCHASE_BATCHES_DIR", {"new": self.directory / "batches"}),
            ("load_profile_inventory_items", {"return_value": []}),
            ("list_profile_entries", {"return_value": [{"id": "p", "display_name": "Account"}]}),
            ("load_steam_account_config_dict", {"return_value": {"steam_id": "76561198000000001"}}),
            ("try_build_product_price_map_from_disk", {"return_value": {}}),
            ("lookup_inventory_item_price_value", {"return_value": None}),
        ):
            patcher = patch.object(purchases, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_history_without_float_uses_only_matching_exterior_reference(self):
        template = SimpleNamespace(steam={"略有磨损": "Test (Minimal Wear)"}, min_float=0.0, max_float=1.0,
                                   weapon_box_id=[1], quality="test", stat_trak=False, paint_index="42")
        prices = {"ordinary": {1: {"test": {0.07: {"42": 2}, 0.15: {"42": 12}, 0.38: {"42": 3}}}}}
        with patch("core.alchemy_quality.resolve_inventory_skin_template", return_value=template):
            result = _snapshot({"market_hash_name": "Test (Minimal Wear)"}, {}, prices)
        self.assertEqual(result["price"], 12)
        self.assertEqual(result["price_source"], "wear_market_reference")
        self.assertIsNone(result["float_value"])

    def test_unknown_exterior_has_no_fabricated_reference(self):
        template = SimpleNamespace(steam={"略有磨损": "Test (Minimal Wear)"}, min_float=0.0, max_float=1.0)
        with patch("core.alchemy_quality.resolve_inventory_skin_template", return_value=template):
            self.assertIsNone(_wear_reference_price({"market_hash_name": "Other"}, {"ordinary": {}}))

    def test_real_events_work_without_any_purchase_or_local_operation(self):
        self.assertEqual(purchases.apply_steam_tradeup_history("p", [event()]), 1)
        record = list_steam_tradeup_records("p")[0]
        self.assertEqual(record["completed_at"], event()["completed_at"])
        self.assertEqual(record["products"][0]["name"], "Actual product")
        self.assertIsNone(record["products"][0]["float_value"])
        self.assertIsNone(record["material_cost"])
        self.assertIsNone(record["profit"])
        self.assertFalse(purchases.list_purchase_batches())

    def test_local_manual_and_automatic_records_alone_do_not_count(self):
        path = purchases.create_purchase_batch("local", profile_id="p", steam_id="", account_name="p", inventory_items=[])
        recipe_id = purchases.add_recipe_to_purchase_batch(path, _special_recipe())
        purchases.set_purchase_batch_recipe_tradeup_completed(path, recipe_id, True)
        self.assertEqual(list_steam_tradeup_records("p"), [])
        batch = purchases.load_purchase_batch(path)
        batch["recipes"][0]["tradeup_execution"] = {"completed_at": "2026-10-26T18:27:00+08:00", "output_asset_ids": ["local-output"]}
        purchases._write_batch(path, batch)
        self.assertEqual(list_steam_tradeup_records("p"), [])
        purchases.apply_steam_tradeup_history("p", [event()])
        self.assertEqual(len(list_steam_tradeup_records("p")), 1)
        purchases.delete_tradeup_completed_batches()
        self.assertEqual(len(list_steam_tradeup_records("p")), 1)

    def test_repeat_sync_is_idempotent_and_identical_crafts_are_preserved(self):
        events = [event(), event()]
        self.assertEqual(purchases.apply_steam_tradeup_history("p", events), 2)
        self.assertEqual(purchases.apply_steam_tradeup_history("p", events), 0)
        self.assertEqual(len(list_steam_tradeup_records("p")), 2)
        # A narrow later refresh must not erase events retained from older pages.
        self.assertEqual(purchases.apply_steam_tradeup_history("p", [event()]), 0)
        self.assertEqual(len(list_steam_tradeup_records("p")), 2)

    def test_explicit_event_ids_and_accounts_are_independent(self):
        rows = [event(event_id="1"), event(event_id="2")]
        purchases.apply_steam_tradeup_history("p", rows)
        purchases.apply_steam_tradeup_history("other", rows)
        self.assertEqual(len(list_steam_tradeup_records()), 4)
        self.assertEqual(len(list_steam_tradeup_records("p")), 2)
        self.assertEqual(purchases.apply_steam_tradeup_history("p", list(reversed(rows))), 0)

    def test_missing_event_time_cannot_be_replaced_with_refresh_time(self):
        purchases.apply_steam_tradeup_history("p", [event()])
        bad = event(completed_at="")
        with self.assertRaisesRegex(ValueError, "实际时间"):
            purchases.apply_steam_tradeup_history("p", [event(event_id="new"), bad])
        self.assertEqual(len(list_steam_tradeup_records("p")), 1)

    def test_reference_values_do_not_invent_profit_for_missing_cost(self):
        purchases.apply_steam_tradeup_history("p", [event()])
        def lookup(item, _prices):
            return 50 if item["name"] == "Actual product" else None
        with patch.object(purchases, "lookup_inventory_item_price_value", side_effect=lookup):
            record = list_steam_tradeup_records("p")[0]
        self.assertEqual(record["output_value"], 50)
        self.assertIsNone(record["material_cost"])
        self.assertIsNone(record["profit"])
        with patch.object(purchases, "lookup_inventory_item_price_value", return_value=2):
            record = list_steam_tradeup_records("p")[0]
        self.assertEqual(record["material_cost"], 20)
        self.assertEqual(record["material_cost_source"], "market_reference")

    def test_parser_rejects_trades_other_games_and_multiple_outputs(self):
        def html(action="Crafted", app=730, output_count=1, input_count=10):
            inputs = '<a class="history_item" data-appid="%s" data-classid="11"></a>' % app
            output = '<a class="history_item" data-appid="%s" data-classid="22"></a>' % app
            return (f'<div class="tradehistoryrow" data-timestamp="1793010420">{action}'
                    '<div class="tradehistory_items_plusminus">-</div>' + inputs * input_count +
                    '<div class="tradehistory_items_plusminus">+</div>' + output * output_count + '</div>')
        rows = parse_steam_inventory_history_page({"html": html() + html("Traded") + html(app=440) + html(output_count=2) + html(input_count=5)})
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]["inputs"]), 5)
        self.assertEqual(history_event_datetime(rows[0]), datetime.fromtimestamp(1793010420, timezone.utc))

    def test_parser_reads_steam_text_time_in_either_date_order(self):
        for text in ("Oct 26, 2026 @ 6:27pm", "6:27pm 26 Oct, 2026"):
            when = history_event_datetime({"text": text})
            self.assertEqual((when.year, when.month, when.day, when.hour, when.minute), (2026, 10, 26, 18, 27))

    def test_parser_reads_date_and_time_elements_without_epoch(self):
        inputs = '<a class="history_item" data-classid="11"></a>' * 10
        html = ('<div class="tradehistoryrow"><div class="tradehistory_date">Oct 26, 2026</div>'
                '<div class="tradehistory_timestamp">6:27pm</div><div>Crafted</div>'
                '<div class="tradehistory_items_plusminus">-</div>' + inputs +
                '<div class="tradehistory_items_plusminus">+</div>'
                '<a class="history_item" data-classid="22"></a></div>')
        rows = parse_steam_inventory_history_page({"html": html})
        self.assertEqual(len(rows), 1)
        self.assertEqual(history_event_datetime(rows[0]).hour, 18)

    def test_statistics_do_not_count_unknown_cost_as_zero_profit(self):
        page = PurchaseManagePage()
        self.addCleanup(page.deleteLater)
        records = [{"output_value": 50, "material_cost": None, "profit": None},
                   {"output_value": 8, "material_cost": 10, "profit": -2}]
        dashboard = page._build_history_dashboard(records, [])
        self.addCleanup(dashboard.deleteLater)
        values = [label.text() for label in dashboard.findChildren(QLabel, "tradeupHistoryMetricValue")]
        self.assertIn("0.0%", values)
        self.assertIn("¥-2.00", values)

    def test_worker_reports_failed_save_as_failure_and_passes_date_range(self):
        worker = SteamHistorySyncWorker(["p"], since=datetime(2026, 9, 1, tzinfo=timezone.utc))
        completed = []
        worker.completed.connect(lambda *args: completed.append(args))
        with patch("ui.workers.steam_history.fetch_steam_tradeup_history", return_value=[event()]) as fetch, patch(
            "ui.workers.steam_history.apply_steam_tradeup_history", side_effect=ValueError("bad time"),
        ):
            worker.run()
        self.assertFalse(completed[0][0])
        self.assertEqual(fetch.call_args.kwargs["since"], worker._since)


if __name__ == "__main__":
    unittest.main()
