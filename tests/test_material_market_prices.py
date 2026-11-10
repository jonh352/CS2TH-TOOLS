import unittest
from unittest.mock import patch

from core.material_market_prices import material_reference_prices


class MaterialReferencePriceTests(unittest.TestCase):
    def test_bundle_buckets_price_each_material_without_platform_requests(self):
        raw = {"fetch_time": "20261110_161758", "ordinary": {"482": {"Classified": {
            "0.38": {"1211": 52}, "0.39": {"1211": 48}, "0.45": {"1211": 36.5},
        }}}}
        assets = [
            {"AssetId": "1", "name": "MP9 | 首冲拿铁（破损不堪）", "float_value": 0.3832},
            {"AssetId": "2", "name": "MP9 | 首冲拿铁（破损不堪）", "float_value": 0.44},
        ]
        with patch("core.material_market_prices.load_product_price_raw", return_value=raw) as load, patch(
            "core.market_candidates.fetch_exact_wear_candidates"
        ) as fetch:
            result = material_reference_prices(assets)
        load.assert_called_once_with()
        fetch.assert_not_called()
        self.assertEqual(result["prices"], {"1": 48, "2": 36.5})
        self.assertEqual(result["fetch_time"], raw["fetch_time"])
        self.assertEqual(result["errors"], [])

    def test_missing_bundle_quote_does_not_use_purchase_cost(self):
        with patch("core.material_market_prices.load_product_price_raw", return_value={}):
            result = material_reference_prices([
                {"AssetId": "1", "name": "MP9 | 首冲拿铁", "float_value": 0.3832, "Price": 99},
            ])
        self.assertEqual(result["prices"], {})
        self.assertEqual(len(result["errors"]), 1)
