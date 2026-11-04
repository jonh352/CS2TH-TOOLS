import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.material_market_prices import material_market_prices


class MaterialMarketPriceTests(unittest.TestCase):
    def test_explicitly_skipped_platforms_are_not_requested(self):
        client = Mock()
        client.market_material_prices.return_value = {"1": 60}
        with patch("core.material_market_prices.fetch_exact_wear_candidates") as fetch:
            result = material_market_prices(
                client, "steam", [dict(AssetId="1", name="test", float_value=0.17)],
                skipped={"buff", "c5"},
            )
        fetch.assert_not_called()
        self.assertEqual(result["platforms"]["1"], {"ECO": 60})

    def test_same_interval_reused_and_lowest_valid_platform_selected(self):
        client = Mock()
        client.market_material_prices.return_value = {"1": 60, "2": 60}
        assets = [dict(AssetId=str(i), name="test", float_value=0.17) for i in (1, 2)]
        template = SimpleNamespace(weapon_name="test", paint_index="1", stat_trak=False)
        with patch("core.material_market_prices.get_template_from_goods_name", return_value=template), patch(
            "core.material_market_prices.fetch_exact_wear_candidates",
            side_effect=lambda provider, **kw: [dict(price=50 if provider == "buff" else 55, float_value=0.16), dict(price=1, float_value=0.2)],
        ) as fetch:
            result = material_market_prices(client, "steam", assets)
        self.assertEqual(result["prices"], {"1": 50, "2": 50})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["platforms"]["1"], {"ECO": 60, "BUFF": 50})

    def test_platform_failures_do_not_become_zero_quotes(self):
        client = Mock()
        client.market_material_prices.side_effect = ValueError("ECO unavailable")
        template = SimpleNamespace(weapon_name="test", paint_index="1", stat_trak=False)
        def fetch_quote(provider, **kwargs):
            if provider == "buff":
                raise ValueError("BUFF login required")
            return [dict(price=55, float_value=0.17)]
        with patch("core.material_market_prices.get_template_from_goods_name", return_value=template), patch(
            "core.material_market_prices.fetch_exact_wear_candidates",
            side_effect=fetch_quote,
        ):
            result = material_market_prices(client, "steam", [dict(AssetId="1", name="test", float_value=0.17)])
        self.assertEqual(result["prices"], {})
        self.assertEqual(result["platforms"]["1"], {})
        self.assertEqual(len(result["errors"]), 2)
