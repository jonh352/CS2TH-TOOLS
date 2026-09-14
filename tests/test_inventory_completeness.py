import unittest
from unittest.mock import Mock, patch

from core.steam.inventory_api import request_steam_inventory_context
from core.steam.inventory_pipeline import process_inventory


class InventoryCompletenessTests(unittest.TestCase):
    def test_asset_without_wear_is_preserved_for_reconciliation(self):
        payload = {"assets": [{"assetid": "123", "classid": "1", "instanceid": "0"}]}
        rows = process_inventory(payload, 0, include_hidden=True)
        self.assertEqual([row["assetid"] for row in rows], ["123"])
        self.assertIsNone(rows[0]["float"])

    def test_incomplete_context_is_not_accepted_as_inventory(self):
        for payload in ({"success": 1, "assets": [], "total_inventory_count": 211},
                        {"success": 1, "more_items": 1}, {}, None):
            with self.subTest(payload=payload), patch(
                "core.steam.inventory_api.requests.get",
                return_value=Mock(status_code=200, json=Mock(return_value=payload)),
            ), patch("core.steam.inventory_api.resolve_system_http_proxy_for_steam", return_value=None):
                with self.assertRaises(RuntimeError):
                    request_steam_inventory_context("", 16, steam_id="test")

    def test_confirmed_empty_context_is_valid(self):
        payload = {"success": 1, "total_inventory_count": 0}
        with patch("core.steam.inventory_api.requests.get",
                   return_value=Mock(status_code=200, json=Mock(return_value=payload))), patch(
            "core.steam.inventory_api.resolve_system_http_proxy_for_steam", return_value=None
        ):
            self.assertEqual(request_steam_inventory_context("", 16, steam_id="test"), payload)
