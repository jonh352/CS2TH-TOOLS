from __future__ import annotations

import json
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from unittest.mock import MagicMock, patch

from core.alchemy_quality import (
    get_name_map,
    get_pid_map,
    resolve_inventory_skin_template,
)
from core.auth_client import (
    Account,
    AuthClient,
    AuthRejectedError,
    AuthSession,
    AuthUnavailableError,
    _account_from_payload,
    has_tradeup_access,
)
from core.float32_wear_prefix import find_float32_range_intersection
from core.alchemy_calc import (
    _filter_recipes_by_break_even_range,
    _fetch_product_price_from_api,
    apply_inventory_buff_prices,
    dinkelbach,
    backfill_missing_substrate_prices,
    build_price_map,
    compute_recipes,
    compute_tradeup_simulation_products,
    eligible_selected_data_for_target,
    filter_non_overlapping_recipes,
    get_expectation_map,
    lookup_pid_price_at_nfv,
    lookup_template_price_value,
    partition_selected_data_by_tradeup_group,
    _sorted_product_nfvs,
)
from core.alchemy_special_wear import (
    _recipe_from_solution,
    _validated_special_wear_recipes,
)
from core.data_utils import SkinInstance, SkinTemplate
from core.platform_links import links_for_recipe_material, links_for_template
from core.market_candidates import (
    c5_signer_collection_scope,
    clear_c5_session_auth,
    clear_provider_auth,
    fetch_buff_candidates,
    fetch_c5_candidates,
    fetch_eco_candidates,
    fetch_exact_wear_candidates,
    fetch_youpin_candidates,
    provider_auth_available,
    save_buff_auth,
    save_c5_auth,
    save_c5_client_headers,
    save_eco_auth,
    save_youpin_auth,
    validate_provider_login,
    validate_youpin_credentials,
    _c5_request_headers,
    _c5_client_headers,
    _collection_max_unit_price,
    _merge_platform_ids,
)
from core.market_external_browser import (
    c5_netlog_login_ready,
    harvest_c5_netlog_headers,
    launch_system_browser,
    wait_browser_closed,
)
from core.product_price_sync import sync_product_price_cache
from core.recipe_bridge import (
    attach_recipe_alternatives,
    cs2th_detail_to_saved_recipe,
    material_wear_range,
    parse_recipe_reference,
    saved_recipe_to_bridge_payload,
)
from core.special_wear_names import get_skin_full_names_without_appearance
from core.version import __version__
from core.special_wear_materials import (
    build_special_wear_materials,
    neighboring_purchase_interval,
)
from core.steam.inventory_pipeline import process_inventory
from ui.pages.inventory import (
    _inventory_item_quality_cn,
    _inventory_item_quality_rank,
    _inventory_status_category,
    _inventory_total_value,
)
from ui.pages.alchemy_simulation import (
    _group_simulation_rows_by_weapon_box,
    _simulation_price_outcome,
)
from ui.workers.market_login import (
    MarketplaceLoginValidationWorker,
    _tokens_from_storage,
)


class MetadataTests(unittest.TestCase):
    @staticmethod
    def _tradeup_rows(
        quality: str,
        stat_trak: bool,
        count: int,
    ) -> tuple[list[dict], SkinTemplate]:
        for goods_name, template in get_name_map().items():
            if (
                template.quality == quality
                and template.stat_trak is stat_trak
                and template.upper_skins
            ):
                float_value = (template.min_float + template.max_float) / 2
                return (
                    [
                        {
                            "goods_id": f"{template.paint_index}-{i}",
                            "goods_name": goods_name,
                            "float_value": float_value,
                            "price": 1.0 + i,
                            "platform": "test",
                        }
                        for i in range(count)
                    ],
                    template,
                )
        raise AssertionError(f"未找到测试模板: {quality=}, {stat_trak=}")

    def test_metadata_and_special_wear_range(self) -> None:
        names = get_skin_full_names_without_appearance()
        self.assertGreater(len(names), 3000)
        template = get_name_map()["AK-47 | 传承"]
        low, high, error = find_float32_range_intersection(
            "0.13", template.min_float, template.max_float
        )
        self.assertIsNone(error)
        self.assertLess(low, high)
        self.assertGreater(len(template.lower_skins), 0)

    def test_break_even_rate_filter_uses_inclusive_minimum_and_maximum(self) -> None:
        recipes = [
            {"name": "low", "break_even_rate": 0.20},
            {"name": "minimum", "break_even_rate": 0.30},
            {"name": "middle", "break_even_rate": 0.50},
            {"name": "maximum", "break_even_rate": 0.70},
            {"name": "high", "break_even_rate": 0.80},
        ]

        filtered = _filter_recipes_by_break_even_range(recipes, 0.30, 0.70)

        self.assertEqual(
            [recipe["name"] for recipe in filtered],
            ["minimum", "middle", "maximum"],
        )

    def test_break_even_rate_filter_rejects_reversed_range(self) -> None:
        recipes = [{"break_even_rate": 0.50}]
        self.assertEqual(
            _filter_recipes_by_break_even_range(recipes, 0.70, 0.30),
            [],
        )

    def test_constrained_search_preserves_expensive_outcome_mix(self) -> None:
        templates_by_outcomes: dict[tuple[str, ...], SkinTemplate] = {}
        for template in get_pid_map().values():
            signature = tuple(sorted(str(pid) for pid in template.upper_skins or []))
            if signature and template.max_float > template.min_float:
                templates_by_outcomes.setdefault(signature, template)
            if len(templates_by_outcomes) >= 2:
                break
        self.assertGreaterEqual(len(templates_by_outcomes), 2)
        template_a, template_b = list(templates_by_outcomes.values())[:2]

        cheap_a = SkinInstance(template_a, normalized_value=0.10, price=1.0)
        cheap_b = SkinInstance(template_a, normalized_value=0.11, price=1.0)
        expensive_mix = SkinInstance(template_b, normalized_value=0.12, price=100.0)
        cheap_a.expectation = 10.0
        cheap_b.expectation = 10.0
        expensive_mix.expectation = 100.0
        substrates = [cheap_a, cheap_b, expensive_mix]

        unconstrained = dinkelbach(
            substrates,
            2,
            0.90,
            return_topk=1,
            preserve_outcome_mix=False,
        )
        constrained = dinkelbach(
            substrates,
            2,
            0.90,
            return_topk=1,
            preserve_outcome_mix=True,
        )
        with patch(
            "core.alchemy_calc._solution_matches_break_even_range",
            side_effect=lambda solution, *_args: expensive_mix in solution,
        ):
            range_constrained = dinkelbach(
                substrates,
                2,
                0.90,
                return_topk=1,
                preserve_outcome_mix=True,
                break_even_range=(0.0, 0.10),
                break_even_price_map={},
            )

        self.assertTrue(unconstrained)
        self.assertTrue(constrained)
        self.assertTrue(range_constrained)
        self.assertFalse(
            any(
                expensive_mix in recipe["solution"]
                for recipe in unconstrained
            )
        )
        self.assertTrue(
            any(
                expensive_mix in recipe["solution"]
                for recipe in constrained
            )
        )
        self.assertTrue(
            all(
                expensive_mix in recipe["solution"]
                for recipe in range_constrained
            )
        )

    def test_break_even_range_activates_constraint_aware_candidate_search(self) -> None:
        rows, _template = self._tradeup_rows("军规级", False, 10)
        with patch("core.alchemy_calc.dinkelbach", return_value=[]) as solve:
            recipes, error = compute_recipes(
                rows,
                {},
                0.50,
                0.50,
                mode="target",
                max_break_even_rate=0.10,
            )

        self.assertIsNone(error)
        self.assertEqual(recipes, [])
        self.assertTrue(solve.call_args.kwargs["preserve_outcome_mix"])

    def test_special_wear_boundary_recipe_uses_simulation_float32_result(self) -> None:
        pid_map = get_pid_map()
        m4 = pid_map["1281"]
        glock = pid_map["1282"]
        output = pid_map["1280"]
        wears = [
            0.103063017129898071,
            0.110177859663963318,
            0.107255019247531891,
            0.115088120102882385,
            0.115885466337203979,
            0.130994781851768494,
            0.122246250510215759,
            0.133971914649009705,
            0.134800836443901062,
            0.137539818882942200,
        ]
        templates = [m4, m4, glock, glock, glock, m4, glock, glock, glock, glock]
        substrates = list(zip(templates, wears))

        error, rows, avg_nfv = compute_tradeup_simulation_products(substrates)
        self.assertIsNone(error)
        self.assertIsNotNone(avg_nfv)
        output_row = next(
            row for row in rows if row["skin_template"].paint_index == "1280"
        )
        self.assertEqual(output_row["float_value"], 0.131451994180679321)

        instances = [
            SkinInstance(template, wear, price=1.0)
            for template, wear in substrates
        ]
        recipe = _recipe_from_solution(instances, 10, {})
        self.assertEqual(recipe["avg_nfv"], avg_nfv)

        low, high, range_error = find_float32_range_intersection(
            "0.1314520", output.min_float, output.max_float
        )
        self.assertIsNone(range_error)
        self.assertEqual(
            _validated_special_wear_recipes([recipe], output, low, high),
            [],
        )

        corrected = list(substrates)
        corrected[8] = (glock, 0.1348012)
        error, corrected_rows, corrected_avg = compute_tradeup_simulation_products(
            corrected
        )
        self.assertIsNone(error)
        corrected_output = next(
            row["float_value"]
            for row in corrected_rows
            if row["skin_template"].paint_index == "1280"
        )
        self.assertTrue(low <= corrected_output <= high)
        corrected_recipe = {"avg_nfv": corrected_avg}
        self.assertEqual(
            _validated_special_wear_recipes(
                [corrected_recipe], output, low, high
            )[0]["special_wear_output_float"],
            corrected_output,
        )

    def test_marketplace_links_use_template_ids(self) -> None:
        template = get_name_map()["AK-47 | 传承"]
        links = links_for_template(template, "崭新出厂")
        self.assertIn("buff.163.com/goods/", links["buff"])
        self.assertIn("steamcommunity.com/market/listings/730/", links["steam"])
        self.assertEqual(set(links), {"buff", "yyyp", "c5", "eco", "steam"})

    def test_exact_wear_candidate_parsers(self) -> None:
        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload
                self.text = "{}"

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        session = MagicMock()
        session.headers = {}
        session.get.return_value = FakeResponse(
            {
                "code": "OK",
                "data": {
                    "items": [
                        {
                            "id": "buff-order-1",
                            "price": "20.5",
                            "asset_info": {"paintwear": "0.164862"},
                        },
                        {
                            "id": "outside",
                            "price": "1",
                            "asset_info": {"paintwear": "0.3"},
                        },
                    ]
                },
            }
        )
        with patch(
            "core.market_candidates.requests.Session",
            return_value=session,
        ):
            buff_rows = fetch_buff_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[956527],
            )
        self.assertEqual(len(buff_rows), 1)
        self.assertEqual(buff_rows[0]["platform"], "buff")
        self.assertAlmostEqual(buff_rows[0]["float_value"], 0.164862)
        buff_params = session.get.call_args.kwargs["params"]
        self.assertEqual(buff_params["min_paintwear"], "0.15")
        self.assertEqual(buff_params["max_paintwear"], "0.179999999")
        self.assertEqual(buff_params["sort_by"], "price.asc")
        self.assertEqual(buff_params["page_size"], 50)
        buff_windows = [
            (
                call.kwargs["params"]["min_paintwear"],
                call.kwargs["params"]["max_paintwear"],
            )
            for call in session.get.call_args_list
        ]
        self.assertEqual(buff_windows, [("0.15", "0.179999999")])
        session.close.assert_called()

        with patch(
            "core.market_candidates.requests.post",
            return_value=FakeResponse(
                {
                    "Code": 0,
                    "Data": {
                        "CommodityList": [
                            {
                                "Id": "youpin-order-1",
                                "Price": "19.9",
                                "Abrade": "0.164862",
                            }
                        ]
                    },
                }
            ),
        ) as youpin_post:
            youpin_rows = fetch_youpin_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[125007],
            )
        self.assertEqual(len(youpin_rows), 1)
        self.assertEqual(youpin_rows[0]["platform"], "yyyp")
        self.assertAlmostEqual(youpin_rows[0]["price"], 19.9)
        wear_windows = [
            (
                call.kwargs["json"]["minAbrade"],
                call.kwargs["json"]["maxAbrade"],
            )
            for call in youpin_post.call_args_list
        ]
        self.assertEqual(
            wear_windows,
            [(0.15, 0.18)],
        )

        def _fake_c5_http_fetch(**kwargs):
            return [
                {
                    "goods_name": kwargs.get("display_name") or "",
                    "float_value": 0.164862,
                    "price": 18.5,
                    "goods_id": "c5:c5-order-1:0.164862",
                    "platform": "c5",
                    "listing_id": "c5-order-1",
                    "purchase_link": "https://www.c5game.com/",
                },
                {
                    "goods_name": kwargs.get("display_name") or "",
                    "float_value": 0.3,
                    "price": 1.0,
                    "goods_id": "c5:outside:0.3",
                    "platform": "c5",
                    "listing_id": "outside",
                    "purchase_link": "https://www.c5game.com/",
                },
            ]

        with patch(
            "core.market_candidates._c5_auth",
            return_value=("c5token=abc12345token; path=/", "c5-access-token-value"),
        ), patch(
            "core.market_candidates._fetch_c5_via_search_api",
            side_effect=_fake_c5_http_fetch,
        ) as http_fetch, patch(
            "core.market_candidates._fetch_c5_via_browser",
            side_effect=_fake_c5_http_fetch,
        ) as browser_fetch:
            c5_rows = fetch_c5_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[1098059387020423168],
            )
        self.assertEqual(len(c5_rows), 1)
        self.assertEqual(c5_rows[0]["platform"], "c5")
        self.assertAlmostEqual(c5_rows[0]["float_value"], 0.164862)
        self.assertEqual(http_fetch.call_count, 0)
        self.assertEqual(browser_fetch.call_count, 1)
        self.assertEqual(
            browser_fetch.call_args.kwargs.get("ids"),
            [1098059387020423168],
        )

        def _fake_c5_browser_fallback(**kwargs):
            return [
                {
                    "goods_name": kwargs.get("display_name") or "",
                    "float_value": 0.155,
                    "price": 22.0,
                    "goods_id": "c5:c5-order-2:0.155",
                    "platform": "c5",
                    "listing_id": "c5-order-2",
                    "purchase_link": "https://www.c5game.com/",
                }
            ]

        with patch(
            "core.market_candidates._c5_auth",
            return_value=("c5token=abc12345token; path=/", "c5-access-token-value"),
        ), patch(
            "core.market_candidates._fetch_c5_via_search_api",
            side_effect=RuntimeError("C5GAME 本地签名器暂时不可用"),
        ) as http_fetch, patch(
            "core.market_candidates._fetch_c5_via_browser",
            side_effect=_fake_c5_browser_fallback,
        ) as browser_fetch:
            fallback_rows = fetch_c5_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[1098059387020423168],
            )
        self.assertEqual(len(fallback_rows), 1)
        self.assertAlmostEqual(fallback_rows[0]["float_value"], 0.155)
        self.assertEqual(http_fetch.call_count, 0)
        self.assertEqual(browser_fetch.call_count, 1)

        class EcoApiResponse:
            status_code = 200
            text = json.dumps(
                {
                    "StatusCode": "0",
                    "StatusData": {
                        "ResultCode": "0",
                        "ResultData": {
                            "PageResult": [
                                {
                                    "GoodsNum": "eco-order-1",
                                    "SellingPrice": "17.20",
                                    "PaintWear": "0.164862",
                                },
                                {
                                    "GoodsNum": "outside",
                                    "SellingPrice": "1.00",
                                    "PaintWear": "0.300000",
                                },
                            ]
                        },
                    },
                }
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        with patch(
            "core.market_candidates._eco_auth",
            return_value=("eco-login-token-value", "loginToken=eco-login-token-value"),
        ), patch(
            "core.market_candidates.requests.post",
            return_value=EcoApiResponse(),
        ) as eco_post:
            eco_rows = fetch_eco_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[7332],
            )
        self.assertEqual(len(eco_rows), 1)
        self.assertEqual(eco_rows[0]["platform"], "eco")
        self.assertAlmostEqual(eco_rows[0]["price"], 17.2)
        self.assertIn(
            "SellGoodsQuery",
            str(eco_post.call_args.args[0]),
        )
        eco_body = eco_post.call_args.kwargs["json"]
        self.assertEqual(eco_body["GoodsId"], 7332)
        self.assertEqual(eco_body["SortType"], 1)
        self.assertEqual(eco_body["Sort"], 0)
        self.assertAlmostEqual(eco_body["StartPaintWear"], 0.15)
        self.assertAlmostEqual(eco_body["EndPaintWear"], 0.18)

        class EcoHashApiResponse:
            status_code = 200
            text = json.dumps(
                {
                    "StatusCode": "0",
                    "StatusData": {
                        "ResultCode": "0",
                        "ResultData": {
                            "PageResult": [
                                {
                                    "GoodsNum": "eco-order-hash",
                                    "SellingPrice": "18.8",
                                    "PaintWear": "0.12",
                                }
                            ]
                        },
                    },
                }
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        hashed = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
            steam={"略有磨损": "USP-S | Torque (Minimal Wear)"},
            eco={"略有磨损": 7332},
        )
        with patch(
            "core.market_candidates._eco_auth",
            return_value=("eco-login-token-value", "loginToken=eco-login-token-value"),
        ), patch(
            "core.market_candidates.requests.post",
            return_value=EcoHashApiResponse(),
        ) as eco_hash_post, patch(
            "core.market_candidates._candidate_cache",
            {},
        ):
            hash_rows = fetch_eco_candidates(
                template=hashed,
                display_name="USP消音版 | 破颚者",
                min_wear=0.10,
                max_wear=0.15,
                max_pages=1,
                request_interval=1,
            )
        self.assertEqual(len(hash_rows), 1)
        self.assertEqual(hash_rows[0]["listing_id"], "eco-order-hash")
        hash_body = eco_hash_post.call_args.kwargs["json"]
        self.assertEqual(hash_body["HashName"], "USP-S | Torque (Minimal Wear)")
        self.assertNotIn("GoodsId", hash_body)

        class EcoSliderApiResponse:
            status_code = 200
            text = json.dumps(
                {
                    "StatusCode": "0",
                    "StatusData": {
                        "ResultCode": "429",
                        "ResultMsg": "slider-guid",
                        "ResultData": None,
                    },
                }
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        class FakeEcoSession:
            def __init__(self) -> None:
                self.list_calls = 0
                self.gate_calls = 0

            def complete_eco_access_gate(self, **kwargs):
                self.gate_calls += 1
                return "cleared"

            def fetch_eco_list(self, **kwargs):
                self.list_calls += 1
                return {}

        from core.market_candidates import EcoPlatformPausedError

        fake_session = FakeEcoSession()
        with patch(
            "core.market_candidates._eco_auth",
            return_value=(
                "eco-login-token-value",
                "loginToken=eco-login-token-value; refreshToken=eco-refresh",
            ),
        ), patch(
            "core.market_candidates.requests.post",
            return_value=EcoSliderApiResponse(),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_access_session.get_access_session",
            return_value=fake_session,
        ), patch(
            "core.market_access_session.close_access_sessions",
        ), patch(
            "core.market_candidates._candidate_cache",
            {},
        ):
            with self.assertRaises(EcoPlatformPausedError):
                fetch_eco_candidates(
                    template=bare,
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=1,
                    extra_ids=[7332],
                    silent=False,
                )
        # ResultMsg "slider-guid" is a clear slider signal → must open gate.
        self.assertGreaterEqual(fake_session.gate_calls, 1)
        self.assertEqual(fake_session.list_calls, 0)

        silent_session = FakeEcoSession()
        with patch(
            "core.market_candidates._eco_auth",
            return_value=(
                "eco-login-token-value",
                "loginToken=eco-login-token-value; refreshToken=eco-refresh",
            ),
        ), patch(
            "core.market_candidates.requests.post",
            return_value=EcoSliderApiResponse(),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_access_session.get_access_session",
            return_value=silent_session,
        ), patch(
            "core.market_access_session.close_access_sessions",
        ), patch(
            "core.market_candidates._candidate_cache",
            {},
        ):
            with self.assertRaises(EcoPlatformPausedError) as raised:
                fetch_eco_candidates(
                    template=bare,
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=1,
                    extra_ids=[7332],
                    silent=True,
                )
        self.assertTrue(
            any(
                marker in str(raised.exception)
                for marker in ("访问校验", "429", "滑块", "暂停")
            )
        )
        self.assertGreaterEqual(silent_session.gate_calls, 1)
        self.assertEqual(silent_session.list_calls, 0)

        # Pure rate-limit (no slider token) → silent retries only, no popup.
        class EcoRateLimitApiResponse:
            status_code = 200
            text = json.dumps(
                {
                    "StatusCode": "0",
                    "StatusData": {
                        "ResultCode": "429",
                        "ResultMsg": "频率过高",
                        "ResultData": None,
                    },
                }
            )

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        rate_session = FakeEcoSession()
        with patch(
            "core.market_candidates._eco_auth",
            return_value=(
                "eco-login-token-value",
                "loginToken=eco-login-token-value; refreshToken=eco-refresh",
            ),
        ), patch(
            "core.market_candidates.requests.post",
            return_value=EcoRateLimitApiResponse(),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_access_session.get_access_session",
            return_value=rate_session,
        ), patch(
            "core.market_access_session.close_access_sessions",
        ), patch(
            "core.market_candidates._candidate_cache",
            {},
        ):
            with self.assertRaises(EcoPlatformPausedError):
                fetch_eco_candidates(
                    template=bare,
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=1,
                    extra_ids=[7332],
                    silent=True,
                )
        self.assertEqual(rate_session.gate_calls, 0)

        class EcoMissingThenOkResponse:
            status_code = 200
            _calls = 0

            @classmethod
            def next_payload(cls) -> dict:
                cls._calls += 1
                if cls._calls == 1:
                    return {
                        "StatusCode": "0",
                        "StatusData": {
                            "ResultCode": "1",
                            "ResultMsg": "商品不存在",
                            "ResultData": None,
                        },
                    }
                return {
                    "StatusCode": "0",
                    "StatusData": {
                        "ResultCode": "0",
                        "ResultData": {
                            "PageResult": [
                                {
                                    "GoodsNum": "eco-order-ok",
                                    "Price": "19.8",
                                    "Abrade": "0.161",
                                }
                            ]
                        },
                    },
                }

            def __init__(self):
                self.text = json.dumps(self.next_payload())

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        with patch(
            "core.market_candidates._eco_auth",
            return_value=("eco-login-token-value", "loginToken=eco-login-token-value"),
        ), patch(
            "core.market_candidates.requests.post",
            side_effect=lambda *a, **k: EcoMissingThenOkResponse(),
        ), patch(
            "core.market_candidates._candidate_cache",
            {},
        ):
            skipped_rows = fetch_eco_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[111, 7332],
            )
        self.assertEqual(len(skipped_rows), 1)
        self.assertEqual(skipped_rows[0]["listing_id"], "eco-order-ok")

    def test_collection_price_cap_helper(self) -> None:
        self.assertEqual(_collection_max_unit_price(10), 20.0)
        self.assertEqual(_collection_max_unit_price("12.5"), 25.0)
        self.assertEqual(
            _collection_max_unit_price(10, multiplier=2.5),
            25.0,
        )
        self.assertIsNone(_collection_max_unit_price(0))
        self.assertIsNone(_collection_max_unit_price(None))
        self.assertIsNone(_collection_max_unit_price("bad"))

    def test_c5_access_gate_verify_vs_silent(self) -> None:
        from core.market_candidates import (
            C5AccessGateError,
            C5PlatformPausedError,
            _c5_message_has_verify_signal,
            _resolve_c5_access_gate,
        )

        self.assertTrue(_c5_message_has_verify_signal("触发风控，请完成安全验证"))
        self.assertFalse(_c5_message_has_verify_signal("网页采集接口暂时不可用"))

        browser_calls: list[str] = []

        def fake_complete(**_kwargs):
            browser_calls.append("opened")

        with patch(
            "core.market_candidates._complete_c5_verify_system_browser",
            side_effect=fake_complete,
        ), patch(
            "core.market_candidates._c5_access_gate_probe_ok",
            return_value=False,
        ), patch(
            "core.market_candidates.interruptible_wait",
        ):
            with self.assertRaises(C5PlatformPausedError):
                _resolve_c5_access_gate(
                    request_interval=1.0,
                    gate_error=C5AccessGateError("请完成安全验证", needs_verify=True),
                )
        self.assertEqual(browser_calls, ["opened"])

        browser_calls.clear()
        with patch(
            "core.market_candidates._complete_c5_verify_system_browser",
            side_effect=fake_complete,
        ), patch(
            "core.market_candidates._c5_access_gate_probe_ok",
            return_value=False,
        ), patch(
            "core.market_candidates.interruptible_wait",
        ) as wait_mock:
            with self.assertRaises(C5PlatformPausedError) as raised:
                _resolve_c5_access_gate(
                    request_interval=1.0,
                    gate_error=C5AccessGateError(
                        "C5GAME 网页采集接口暂时不可用（登录仍有效）。",
                        needs_verify=False,
                    ),
                )
        self.assertEqual(browser_calls, [])
        self.assertGreaterEqual(wait_mock.call_count, 2)
        self.assertIn("静默重试", str(raised.exception))

    def test_eco_filters_and_stops_at_price_cap(self) -> None:
        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )
        calls: list[int] = []

        class EcoPageResponse:
            status_code = 200

            def __init__(self, page: int):
                self._page = page
                if page == 1:
                    items = [
                        {
                            "GoodsNum": "eco-cheap",
                            "SellingPrice": "5.0",
                            "PaintWear": "0.16",
                        },
                        {
                            "GoodsNum": "eco-mid",
                            "SellingPrice": "7.0",
                            "PaintWear": "0.161",
                        },
                    ]
                else:
                    items = [
                        {
                            "GoodsNum": "eco-expensive",
                            "SellingPrice": "30.0",
                            "PaintWear": "0.162",
                        }
                    ]
                self.text = json.dumps(
                    {
                        "StatusCode": "0",
                        "StatusData": {
                            "ResultCode": "0",
                            "ResultData": {"PageResult": items},
                        },
                    }
                )

            @staticmethod
            def raise_for_status() -> None:
                return None

            def json(self):
                return json.loads(self.text)

        def post_side_effect(*_args, **kwargs):
            page = int(kwargs["json"]["PageIndex"])
            calls.append(page)
            return EcoPageResponse(page)

        with patch(
            "core.market_candidates._eco_auth",
            return_value=("eco-login-token-value", "loginToken=eco-login-token-value"),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_candidates.requests.post",
            side_effect=post_side_effect,
        ):
            rows = fetch_eco_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=0,
                request_interval=1,
                extra_ids=[7332],
                max_unit_price=25.0,
            )
        self.assertEqual([row["listing_id"] for row in rows], ["eco-cheap", "eco-mid"])
        self.assertEqual(calls, [1, 2])

        calls.clear()
        with patch(
            "core.market_candidates._eco_auth",
            return_value=("eco-login-token-value", "loginToken=eco-login-token-value"),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_candidates.requests.post",
            side_effect=post_side_effect,
        ):
            rows = fetch_eco_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=0,
                request_interval=1,
                extra_ids=[7332],
                max_unit_price=6.0,
            )
        self.assertEqual([row["listing_id"] for row in rows], ["eco-cheap"])
        # Page 1 keeps cheap; page 2 is entirely over cap → early stop (no page 3).
        self.assertEqual(calls, [1, 2])

    def test_buff_stops_paging_after_price_cap(self) -> None:
        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        pages = [
            FakeResponse(
                {
                    "code": "OK",
                    "data": {
                        "total_page": 5,
                        "items": [
                            {
                                "id": "cheap",
                                "price": "15",
                                "asset_info": {"paintwear": "0.16"},
                            }
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "code": "OK",
                    "data": {
                        "total_page": 5,
                        "items": [
                            {
                                "id": "expensive",
                                "price": "21",
                                "asset_info": {"paintwear": "0.161"},
                            }
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "code": "OK",
                    "data": {
                        "total_page": 5,
                        "items": [
                            {
                                "id": "should-not-fetch",
                                "price": "22",
                                "asset_info": {"paintwear": "0.162"},
                            }
                        ],
                    },
                }
            ),
        ]

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = pages
        with patch(
            "core.market_candidates.requests.Session",
            return_value=session,
        ):
            rows = fetch_buff_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=0,
                request_interval=1,
                extra_ids=[956527],
                max_unit_price=20.0,
            )
        self.assertEqual([row["listing_id"] for row in rows], ["cheap"])
        self.assertEqual(session.get.call_count, 2)

    def test_buff_rate_limit_respects_retry_after(self) -> None:
        from core.market_candidates import _BUFF_RATE_LIMIT_DEFAULT_BACKOFF_SECONDS

        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class RateLimitedResponse:
            status_code = 429
            headers = {"Retry-After": "12"}

            def raise_for_status(self):
                raise AssertionError("should not raise_for_status on 429")

            def json(self):
                raise AssertionError("should not json on 429")

        class OkResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": "OK",
                    "data": {
                        "items": [
                            {
                                "id": "after-retry",
                                "price": "18",
                                "asset_info": {"paintwear": "0.16"},
                            }
                        ]
                    },
                }

        waits: list[float] = []
        session = MagicMock()
        session.headers = {}
        session.get.side_effect = [RateLimitedResponse(), OkResponse()]
        with (
            patch(
                "core.market_candidates.requests.Session",
                return_value=session,
            ),
            patch(
                "core.market_candidates.interruptible_wait",
                side_effect=lambda seconds, _cancel=None: waits.append(float(seconds)),
            ),
        ):
            rows = fetch_buff_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
                extra_ids=[956527],
            )
        self.assertEqual([row["listing_id"] for row in rows], ["after-retry"])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(waits, [12.0])
        self.assertNotEqual(waits[0], _BUFF_RATE_LIMIT_DEFAULT_BACKOFF_SECONDS[0])

    def test_buff_rate_limit_uses_default_backoff_without_retry_after(self) -> None:
        from core.market_candidates import (
            _BUFF_RATE_LIMIT_DEFAULT_BACKOFF_SECONDS,
            _BUFF_RATE_LIMIT_RETRIES,
        )

        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class RateLimitedResponse:
            status_code = 429
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {}

        waits: list[float] = []
        session = MagicMock()
        session.headers = {}
        session.get.side_effect = [
            RateLimitedResponse() for _ in range(_BUFF_RATE_LIMIT_RETRIES + 1)
        ]
        with (
            patch(
                "core.market_candidates.requests.Session",
                return_value=session,
            ),
            patch(
                "core.market_candidates.interruptible_wait",
                side_effect=lambda seconds, _cancel=None: waits.append(float(seconds)),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "访问频率过高"):
                fetch_buff_candidates(
                    template=bare,
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=1,
                    extra_ids=[956527],
                )
        self.assertEqual(
            waits,
            list(_BUFF_RATE_LIMIT_DEFAULT_BACKOFF_SECONDS[:_BUFF_RATE_LIMIT_RETRIES]),
        )
        self.assertEqual(session.get.call_count, _BUFF_RATE_LIMIT_RETRIES + 1)
        session.close.assert_called()

    def test_youpin_stops_at_wear_window_row_limit(self) -> None:
        from core.market_candidates import _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW

        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        page_size = 40
        pages_needed = (_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW // page_size) + 2

        def make_page(page_index: int) -> FakeResponse:
            start = (page_index - 1) * page_size
            items = [
                {
                    "Id": f"yp-{start + i}",
                    "Price": "10",
                    "Abrade": f"{0.16 + (i % 10) * 0.0001:.6f}",
                }
                for i in range(page_size)
            ]
            return FakeResponse(
                {"Code": 0, "Data": {"CommodityList": items}}
            )

        responses = [make_page(i) for i in range(1, pages_needed + 1)]
        with patch(
            "core.market_candidates._youpin_auth",
            return_value=("tok", ""),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_candidates.requests.post",
            side_effect=responses,
        ) as youpin_post:
            rows = fetch_youpin_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=0,
                request_interval=1,
                extra_ids=[125007],
            )
        self.assertEqual(len(rows), _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW)
        self.assertLessEqual(
            youpin_post.call_count,
            (_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW // page_size) + 1,
        )

    def test_youpin_continues_when_first_page_outside_wear_window(self) -> None:
        """Full first page filtered by wear must not abort paging (unlike empty list)."""
        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        page_size = 40
        out_of_window = FakeResponse(
            {
                "Code": 0,
                "Data": {
                    "CommodityList": [
                        {
                            "Id": f"yp-out-{i}",
                            "Price": "10",
                            "Abrade": "0.05",
                        }
                        for i in range(page_size)
                    ]
                },
            }
        )
        in_window = FakeResponse(
            {
                "Code": 0,
                "Data": {
                    "CommodityList": [
                        {
                            "Id": "yp-in-1",
                            "Price": "10",
                            "Abrade": "0.16",
                        }
                    ]
                },
            }
        )
        with patch(
            "core.market_candidates._youpin_auth",
            return_value=("tok", ""),
        ), patch(
            "core.market_candidates.interruptible_wait",
        ), patch(
            "core.market_candidates.requests.post",
            side_effect=[out_of_window, in_window],
        ) as youpin_post:
            rows = fetch_youpin_candidates(
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=0,
                request_interval=1,
                extra_ids=[125007],
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["listing_id"], "yp-in-1")
        self.assertEqual(youpin_post.call_count, 2)

    def test_fetch_exact_wear_applies_price_cap_for_buff_and_eco(self) -> None:
        template = get_name_map()["USP消音版 | 破颚者"]
        bare = SkinTemplate(
            paint_index=template.paint_index,
            weapon_name=template.weapon_name,
            skin_name=template.skin_name,
            quality=template.quality,
            stat_trak=template.stat_trak,
            min_float=template.min_float,
            max_float=template.max_float,
        )
        with patch(
            "core.market_candidates.provider_auth_available",
            return_value=True,
        ), patch(
            "core.market_candidates._candidate_cache",
            {},
        ), patch(
            "core.market_candidates.fetch_buff_candidates",
            return_value=[],
        ) as buff_fetch, patch(
            "core.market_candidates.fetch_eco_candidates",
            return_value=[],
        ) as eco_fetch:
            fetch_exact_wear_candidates(
                "buff",
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=2,
                unit_price_cny=10,
            )
            fetch_exact_wear_candidates(
                "eco",
                template=bare,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=2,
                unit_price_cny=10,
                silent=True,
            )
        self.assertEqual(buff_fetch.call_args.kwargs["max_unit_price"], 20.0)
        self.assertEqual(eco_fetch.call_args.kwargs["max_unit_price"], 25.0)
        self.assertTrue(eco_fetch.call_args.kwargs["silent"])

    def test_wear_windows_split_by_exterior_buckets(self) -> None:
        from core.market_candidates import _split_wear_windows

        self.assertEqual(
            _split_wear_windows(0.11, 0.27),
            [(0.11, 0.15), (0.15, 0.27)],
        )
        self.assertEqual(
            _split_wear_windows(0.15, 0.18),
            [(0.15, 0.18)],
        )
        self.assertEqual(
            _split_wear_windows(0.05, 0.40),
            [(0.05, 0.07), (0.07, 0.15), (0.15, 0.38), (0.38, 0.40)],
        )

    def test_youpin_queries_exterior_windows_only(self) -> None:
        from core.market_candidates import fetch_youpin_candidates

        source = get_name_map()["USP消音版 | 破颚者"]
        template = SkinTemplate(
            paint_index=source.paint_index,
            weapon_name=source.weapon_name,
            skin_name=source.skin_name,
            quality=source.quality,
            stat_trak=source.stat_trak,
            min_float=source.min_float,
            max_float=source.max_float,
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, payload: dict):
                self._payload = payload
                self.text = "{}"

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._payload

        requested: list[tuple[float, float]] = []

        def post_side_effect(*_args, **kwargs):
            body = kwargs["json"]
            low = float(body["minAbrade"])
            high = float(body["maxAbrade"])
            requested.append((low, high))
            midpoint = (low + high) / 2
            return FakeResponse(
                {
                    "Code": 0,
                    "Data": {
                        "CommodityList": [
                            {
                                "Id": f"order-{low}-{high}",
                                "Price": "12.5",
                                "Abrade": str(midpoint),
                            }
                        ]
                    },
                }
            )

        with (
            patch("core.market_candidates.requests.post", side_effect=post_side_effect),
            patch("core.market_candidates.interruptible_wait"),
        ):
            rows = fetch_youpin_candidates(
                template=template,
                display_name="USP消音版 | 破颚者",
                min_wear=0.11,
                max_wear=0.27,
                max_pages=1,
                request_interval=1,
                extra_ids=[125007],
            )

        self.assertEqual(requested, [(0.11, 0.15), (0.15, 0.27)])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(0.11 <= row["float_value"] < 0.27 for row in rows))

    def test_buff_login_validation_uses_real_platform_response(self) -> None:
        class FakeResponse:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"code": "OK", "data": {"items": []}}

        with (
            patch(
                "core.market_candidates._buff_cookie",
                return_value="session=valid-session; csrf_token=token",
            ),
            patch(
                "core.market_candidates.requests.get",
                return_value=FakeResponse(),
            ) as get,
        ):
            result = validate_provider_login("buff")

        self.assertTrue(result["ok"])
        self.assertIn("Cookie", get.call_args.kwargs["headers"])
        params = get.call_args.kwargs["params"]
        self.assertIn("min_paintwear", params)
        self.assertIn("max_paintwear", params)

    def test_buff_login_validation_rejects_unauthenticated_wear_query(self) -> None:
        class FakeResponse:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"code": "Login Required", "error": "请先登录"}

        with (
            patch(
                "core.market_candidates._buff_cookie",
                return_value="session=invalid-session; csrf_token=token",
            ),
            patch(
                "core.market_candidates.requests.get",
                return_value=FakeResponse(),
            ),
        ):
            result = validate_provider_login("buff")

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("登录", result["message"])

    def test_buff_login_validation_rejects_browser_only_hint(self) -> None:
        with patch("core.market_candidates._buff_cookie", return_value=""):
            result = validate_provider_login("buff")

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("APP 未获取", result["message"])

    def test_youpin_login_validation_returns_account_name(self) -> None:
        class CollectionResponse:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"Code": 0, "Data": {"CommodityList": []}}

        class ProfileResponse:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "Code": 0,
                    "Data": {"UserId": 7, "NickName": "测试用户"},
                }

        with (
            patch(
                "core.market_candidates._youpin_auth",
                return_value=("valid-token", "yp_cookie=1"),
            ),
            patch(
                "core.market_candidates.requests.post",
                return_value=CollectionResponse(),
            ) as post,
            patch(
                "core.market_candidates.requests.get",
                return_value=ProfileResponse(),
            ),
        ):
            result = validate_provider_login("yyyp")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_name"], "测试用户")
        body = post.call_args.kwargs["json"]
        self.assertIn("minAbrade", body)
        self.assertIn("maxAbrade", body)

    def test_c5_login_validation_rejects_missing_credentials(self) -> None:
        with patch(
            "core.market_candidates._c5_openapi_auth",
            return_value=("", ""),
        ), patch("core.market_candidates._c5_auth", return_value=("", "")):
            result = validate_provider_login("c5")

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("APP 未获取", result["message"])

    def test_c5_session_clear_keeps_client_headers(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            save_c5_client_headers(
                {
                    "x-device-id": "persist-device-123",
                    "x-area": "1",
                }
            )
            save_c5_auth("C5Token=session-value-12345", "c5-access-token-value")
            profile = Path(directory) / "market_browser_profiles" / "c5"
            profile.mkdir(parents=True)
            (profile / "Cookies").write_text("placeholder", encoding="utf-8")

            result = clear_c5_session_auth()

            self.assertTrue(result["ok"])
            self.assertFalse(provider_auth_available("c5"))
            self.assertFalse(profile.exists())
            headers = _c5_client_headers()
            self.assertEqual(headers.get("x-device-id"), "persist-device-123")
            self.assertEqual(headers.get("x-area"), "1")

    def test_c5_signer_reused_within_collection_scope(self) -> None:
        from core.market_candidates import _fetch_c5_via_search_api

        enter_count = 0
        close_count = 0

        class FakeSigner:
            def __enter__(self) -> "FakeSigner":
                nonlocal enter_count
                enter_count += 1
                return self

            def __exit__(self, *_args) -> None:
                return None

            def close(self) -> None:
                nonlocal close_count
                close_count += 1

            def sign(self, *_args, **_kwargs) -> str:
                return "fake-signature"

        class SearchApiResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = '{"success":true}'

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "success": True,
                    "errorCode": 0,
                    "data": {
                        "list": [
                            {
                                "id": "c5-order-scope",
                                "price": "18.5",
                                "assetInfo": {"wear": "0.164862"},
                            }
                        ]
                    },
                }

        fetch_kwargs = {
            "ids": [1098059387020423168],
            "display_name": "USP消音版 | 破颚者",
            "min_wear": 0.15,
            "max_wear": 0.18,
            "max_pages": 1,
            "request_interval": 0,
            "cookie": "c5token=abc12345token; path=/",
            "token": "c5-access-token-value",
        }
        patches = (
            patch(
                "core.market_candidates._c5_client_headers",
                return_value={
                    "x-device-id": "device-scope-123",
                    "x-app-channel": "WEB",
                    "x-source": "1",
                    "x-area": "1",
                },
            ),
            patch(
                "core.market_candidates.requests.get",
                return_value=SearchApiResponse(),
            ),
            patch("core.c5_web_signer.C5WebSigner", FakeSigner),
            patch("core.market_candidates.interruptible_wait"),
        )

        with patches[0], patches[1], patches[2], patches[3]:
            with c5_signer_collection_scope():
                _fetch_c5_via_search_api(**fetch_kwargs)
                _fetch_c5_via_search_api(**fetch_kwargs)
        self.assertEqual(enter_count, 1)
        self.assertEqual(close_count, 1)

        enter_count = 0
        close_count = 0
        with patches[0], patches[1], patches[2], patches[3]:
            _fetch_c5_via_search_api(**fetch_kwargs)
            _fetch_c5_via_search_api(**fetch_kwargs)
        self.assertEqual(enter_count, 2)
        self.assertEqual(close_count, 2)

    def test_c5_browser_risk_pauses_platform(self) -> None:
        from core.market_candidates import C5AccessGateError, C5PlatformPausedError
        from core import market_candidates as mc

        source = get_name_map()["USP消音版 | 破颚者"]
        template = SkinTemplate(
            paint_index=source.paint_index,
            weapon_name=source.weapon_name,
            skin_name=source.skin_name,
            quality=source.quality,
            stat_trak=source.stat_trak,
            min_float=source.min_float,
            max_float=source.max_float,
        )
        with patch(
            "core.market_candidates._c5_auth",
            return_value=("c5token=abc12345token; path=/", "c5-access-token-value"),
        ), patch(
            "core.market_candidates._fetch_c5_via_browser",
            side_effect=C5PlatformPausedError("C5GAME 采集失败，本轮已停止该平台"),
        ):
            with self.assertRaises(C5PlatformPausedError) as raised:
                fetch_c5_candidates(
                    template=template,
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=0,
                    extra_ids=[1098059387020423168],
                )
        self.assertIn("停止", str(raised.exception))

        class FakeCollector:
            def ensure_open(self, **_kwargs):
                return None

            def fetch_list_payload(self, **_kwargs):
                raise C5AccessGateError("请完成安全验证", needs_verify=True)

        with patch(
            "core.c5_browser_collect.get_c5_browser_collector",
            return_value=FakeCollector(),
        ), patch(
            "core.market_candidates._complete_c5_verify_system_browser",
        ) as verify_mock:
            with self.assertRaises(C5PlatformPausedError):
                mc._fetch_c5_via_browser(
                    ids=[1098059387020423168],
                    display_name="USP消音版 | 破颚者",
                    min_wear=0.15,
                    max_wear=0.18,
                    max_pages=1,
                    request_interval=0,
                )
        # Failures stop immediately — no verify popup / retry.
        self.assertEqual(verify_mock.call_count, 0)

    def test_c5_login_validation_uses_platform_response(self) -> None:
        with (
            patch(
                "core.market_candidates._c5_auth",
                return_value=("C5Token=abc12345678; path=/", "c5-access-token-value"),
            ),
            patch(
                "core.market_candidates._probe_c5_collection_login",
                return_value={
                    "provider": "c5",
                    "ok": True,
                    "indeterminate": False,
                    "message": "C5GAME 登录有效",
                },
            ),
            patch(
                "core.market_candidates._probe_c5_account_login",
                return_value={
                    "provider": "c5",
                    "ok": True,
                    "indeterminate": False,
                    "message": "C5GAME 登录有效",
                    "account_name": "C5用户",
                    "user_id": 12,
                },
            ),
        ):
            result = validate_provider_login("c5")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_name"], "C5用户")

    def test_c5_login_validation_rejects_sell_list_false_positive(self) -> None:
        with (
            patch(
                "core.market_candidates._c5_auth",
                return_value=("C5Token=stale-session; path=/", "stale-access-token"),
            ),
            patch(
                "core.market_candidates._probe_c5_collection_login",
                return_value={
                    "provider": "c5",
                    "ok": True,
                    "indeterminate": False,
                    "message": "C5GAME 登录有效",
                },
            ),
            patch(
                "core.market_candidates._probe_c5_account_login",
                return_value={
                    "provider": "c5",
                    "ok": False,
                    "indeterminate": False,
                    "message": "C5GAME 登录已失效，请重新登录",
                },
            ),
        ):
            result = validate_provider_login("c5")

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("登录已失效", result["message"])

    def test_c5_account_probe_rejects_logged_out_response(self) -> None:
        from core.market_candidates import _probe_c5_account_login

        class LoggedOutResponse:
            status_code = 200
            text = '{"code":401,"msg":"请先登录"}'

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"code": 401, "msg": "请先登录"}

        with patch(
            "core.market_candidates.requests.get",
            return_value=LoggedOutResponse(),
        ):
            result = _probe_c5_account_login(
                "C5Token=stale-session",
                "stale-access-token",
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("登录已失效", result["message"])

    def test_eco_login_validation_returns_account_name(self) -> None:
        with (
            patch(
                "core.market_candidates._eco_auth",
                return_value=("eco-token-value-that-is-long", "eco_cookie=1"),
            ),
            patch(
                "core.market_candidates._probe_eco_collection_login",
                return_value={
                    "provider": "eco",
                    "ok": True,
                    "indeterminate": False,
                    "message": "ECOSteam 登录有效",
                },
            ),
            patch(
                "core.market_candidates._lookup_eco_user_profile",
                return_value=("ECO用户", 9),
            ),
        ):
            result = validate_provider_login("eco")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_name"], "ECO用户")

    def test_c5_eco_auth_persist_and_clear(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            c5 = save_c5_auth(
                "C5Token=session-value-12345",
                "c5-token-abcdefghijklmnop",
                nickname="C5用户",
                user_id=12,
            )
            eco = save_eco_auth(
                "eco-token-abcdefghijklmnop",
                "eco_cookie=1",
                nickname="ECO用户",
                user_id=9,
            )
            clear_c5 = clear_provider_auth("c5")
            clear_eco = clear_provider_auth("eco")

        self.assertTrue(c5["ok"])
        self.assertTrue(eco["ok"])
        self.assertTrue(clear_c5["ok"])
        self.assertTrue(clear_eco["ok"])

    def test_c5_client_headers_reuse_and_clear(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            save_c5_client_headers(
                {
                    "App-Version": "6.1.2",
                    "platform": "2",
                    "User-Agent": "Mozilla/5.0 C5Test",
                    "x-area": "1",
                }
            )
            headers = _c5_request_headers(
                item_id=1,
                cookie="C5Token=abc12345678",
                token="c5-access-token-value",
                min_wear=0.15,
                max_wear=0.18,
            )
            # App-Version must never be sent — it triggers C5 error 102.
            self.assertNotIn("App-Version", headers)
            self.assertEqual(headers["User-Agent"], "Mozilla/5.0 C5Test")
            self.assertEqual(headers["x-area"], "1")
            self.assertEqual(headers["x-app-channel"], "WEB")
            self.assertEqual(headers["x-source"], "1")
            self.assertEqual(headers["x-access-token"], "c5-access-token-value")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(
                headers["Accept-Encoding"],
                "gzip, br, zstd, deflate",
            )
            clear_provider_auth("c5")
            headers_after = _c5_request_headers(
                item_id=1,
                cookie="C5Token=abc12345678",
                token="c5-access-token-value",
                min_wear=0.15,
                max_wear=0.18,
            )
            self.assertNotIn("App-Version", headers_after)
            self.assertEqual(headers_after["x-app-channel"], "WEB")
            self.assertEqual(headers_after["x-area"], "1")
            self.assertEqual(
                headers_after["Accept-Encoding"],
                "gzip, br, zstd, deflate",
            )

    def test_c5_request_uses_access_token_from_cookie(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            saved = save_c5_auth(
                "NC5_uid=7; NC5_accessToken=c5-cookie-token-value; theme=light"
            )
            headers = _c5_request_headers(
                item_id=1,
                cookie="NC5_uid=7; NC5_accessToken=c5-cookie-token-value; theme=light",
                token="",
                min_wear=0.15,
                max_wear=0.18,
                timestamp_ms="1785558000000",
                device_id="real-device-123",
                signature="signed-request",
            )

        self.assertEqual(saved["token"], "c5-cookie-token-value")
        self.assertEqual(headers["x-access-token"], "c5-cookie-token-value")
        self.assertEqual(headers["x-start-req-time"], "1785558000000")
        self.assertEqual(headers["x-device-id"], "real-device-123")
        self.assertEqual(headers["x-sign"], "signed-request")

    def test_c5_netlog_extracts_only_reusable_client_markers(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "netlog.json"
            path.write_text(
                '{"request_headers":{"headers":['
                '"x-device-id: real-device-123",'
                '"x-device-model: Edge 150.0.0.0",'
                '"x-sign: must-not-be-reused",'
                '"x-access-token: must-not-be-stored-123456",'
                '"x-area: 1"]},"source":{"id":77,"type":1}},'
                '{"params":{"headers":["HTTP/1.1 200",'
                '"content-type: application/json"]},'
                '"source":{"id":77,"type":1}}',
                encoding="utf-8",
            )
            headers = harvest_c5_netlog_headers(path)
            login_ready = c5_netlog_login_ready(path)

        self.assertEqual(headers["x-device-id"], "real-device-123")
        self.assertEqual(headers["x-device-model"], "Edge 150.0.0.0")
        self.assertEqual(headers["x-area"], "1")
        self.assertNotIn("x-sign", headers)
        self.assertNotIn("x-access-token", headers)
        self.assertTrue(login_ready)

    def test_c5_login_browser_size_and_auto_close(self) -> None:
        sentinel = object()
        with TemporaryDirectory() as directory, patch(
            "core.market_external_browser.resolve_system_browser_executable",
            return_value=Path("C:/Program Files/Edge/msedge.exe"),
        ), patch(
            "core.market_external_browser.clear_chromium_session_restore"
        ), patch(
            "core.market_external_browser._clear_stale_profile_singletons"
        ), patch(
            "core.market_external_browser.subprocess.Popen",
            return_value=sentinel,
        ) as popen:
            result = launch_system_browser(
                profile_dir=Path(directory),
                url="https://www.c5game.com/login",
                net_log_path=Path(directory) / "netlog.json",
            )

        self.assertIs(result, sentinel)
        args = popen.call_args.args[0]
        self.assertIn("--window-size=1280,840", args)

        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.closed = False

            def poll(self):
                return 0 if self.closed else None

            def terminate(self) -> None:
                self.closed = True

            def wait(self, timeout=None):
                self.closed = True
                return 0

        fake = FakeProcess()

        def close_window(process) -> bool:
            process.closed = True
            return True

        with patch(
            "core.market_external_browser._close_browser_window",
            side_effect=close_window,
        ):
            closed = wait_browser_closed(
                fake,
                auto_close_when=lambda: True,
                auto_close_message="登录成功",
            )
        self.assertTrue(closed)

    def test_market_login_capture_persists_only_usable_credentials(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            rejected = save_buff_auth("csrf_token=only")
            accepted = save_buff_auth("session=buff-session; csrf_token=token")
            youpin = save_youpin_auth(
                "youpin-token-value-that-is-long-enough",
                "yp_cookie=1",
                nickname="测试用户",
                user_id=7,
            )
            buff_payload = (
                Path(directory) / "market_auth" / "buff_cookie.json"
            ).read_text(encoding="utf-8")
            youpin_payload = (
                Path(directory) / "market_auth" / "youpin_auth.json"
            ).read_text(encoding="utf-8")

        self.assertFalse(rejected["ok"])
        self.assertTrue(accepted["ok"])
        self.assertTrue(youpin["ok"])
        self.assertIn("session=buff-session", buff_payload)
        self.assertIn('"nickname": "测试用户"', youpin_payload)

    def test_clear_market_login_forgets_credential_and_browser_profile(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "core.market_candidates.CACHE_DIR",
            Path(directory),
        ):
            save_buff_auth("session=buff-session; csrf_token=token")
            profile = Path(directory) / "market_browser_profiles" / "buff"
            profile.mkdir(parents=True)
            (profile / "Cookies").write_text("placeholder", encoding="utf-8")

            result = clear_provider_auth("buff")
            payload = (
                Path(directory) / "market_auth" / "buff_cookie.json"
            ).read_text(encoding="utf-8")

            self.assertTrue(result["ok"])
            self.assertFalse(provider_auth_available("buff"))
            self.assertFalse(profile.exists())
            self.assertIn('"cleared": true', payload)

    def test_c5_validation_uses_collection_probe(self) -> None:
        results: list[dict] = []
        worker = MarketplaceLoginValidationWorker(["c5"])
        worker.provider_checked.connect(
            lambda _provider, result: results.append(dict(result))
        )
        with patch(
            "ui.workers.market_login.validate_provider_login",
            return_value={
                "provider": "c5",
                "ok": True,
                "message": "C5GAME 登录有效",
            },
        ) as validate_mock:
            worker.run()
        validate_mock.assert_called_once_with("c5", timeout=5.0)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])

    def test_eco_validation_uses_collection_probe(self) -> None:
        results: list[dict] = []
        worker = MarketplaceLoginValidationWorker(["eco"])
        worker.provider_checked.connect(
            lambda _provider, result: results.append(dict(result))
        )
        with patch(
            "ui.workers.market_login.validate_provider_login",
            return_value={
                "provider": "eco",
                "ok": False,
                "message": "ECOSteam 登录已失效，请重新登录",
            },
        ) as validate_mock:
            worker.run()
        validate_mock.assert_called_once_with("eco", timeout=5.0)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("登录", results[0]["message"])

    def test_login_validation_checks_providers_in_parallel(self) -> None:
        order: list[str] = []
        started = time.monotonic()

        def fake_validate(provider: str, timeout: float = 5.0) -> dict:
            order.append(f"start:{provider}")
            time.sleep(0.25)
            order.append(f"end:{provider}")
            return {"provider": provider, "ok": True, "message": provider}

        results: list[str] = []
        worker = MarketplaceLoginValidationWorker(["buff", "yyyp", "c5", "eco"])
        worker.provider_checked.connect(
            lambda provider, _result: results.append(provider)
        )
        with patch(
            "ui.workers.market_login.validate_provider_login",
            side_effect=lambda provider, timeout=5.0: fake_validate(
                provider, timeout
            ),
        ):
            worker.run()
        elapsed = time.monotonic() - started
        self.assertEqual(sorted(results), ["buff", "c5", "eco", "yyyp"])
        # Sequential would be ~1.0s; parallel should finish near one sleep.
        self.assertLess(elapsed, 0.8)
        self.assertTrue(any(item.startswith("start:") for item in order))

    def test_youpin_login_capture_extracts_only_token_fields(self) -> None:
        tokens = _tokens_from_storage(
            {
                "local": {
                    "themePreference": "this-is-not-a-login-token-value",
                    "deviceId": "abcdef0123456789abcdef0123456789-1-1710000000-1710000001",
                    "userAuth": '{"accessToken":"captured-token-value-1234567890"}',
                }
            }
        )

        self.assertEqual(tokens, ["captured-token-value-1234567890"])

    def test_recipe_bridge_parses_link_and_material_ranges(self) -> None:
        recipe_id, market = parse_recipe_reference(
            "https://cs2th.cn/recipe/8493d6b6224c0000?market=spot"
        )
        low, high, label = material_wear_range(
            {"float_range": "0.07000000 ~ 0.08499999"}
        )
        links = links_for_recipe_material(
            {
                "goods_id": 123,
                "c5_id": 456,
                "youpin_id": 789,
                "eco_id": 321,
                "market_hash_name_en": "AK-47 | Test (Minimal Wear)",
            },
            min_wear=low,
            max_wear=high,
        )

        self.assertEqual((recipe_id, market), ("8493d6b6224c0000", "spot"))
        self.assertEqual((low, high), (0.07, 0.08499999))
        self.assertIn("0.07000000", label)
        self.assertIn("min_paintwear=0.07000000", links["buff"])
        self.assertIn("maxWear=0.08499999", links["c5"])

        converted = cs2th_detail_to_saved_recipe(
            {
                "_recipe_id": recipe_id,
                "_market": market,
                "input_cost": 20,
                "expected_output": 25,
                "profit_probability": 0.6,
                "avg_input_normalized": 0.2,
                "inputs": [
                    {
                        "name": "USP消音版 | 破额者",
                        "count": 2,
                        "unit_float": 0.164861929,
                        "unit_price_cny": 10,
                        "collection_name": "测试收藏品",
                    }
                ],
                "outcomes": [],
            }
        )
        self.assertEqual(len(converted["substrates_display"]), 2)
        self.assertEqual(converted["simulation_slot_count"], 2)
        self.assertAlmostEqual(converted["rate"], 0.25)

    def test_saved_recipe_to_bridge_payload_and_alternatives(self) -> None:
        name_map = get_name_map()
        sample = next(iter(name_map.values()))
        full_name = f"{sample.weapon_name} | {sample.skin_name}"
        box = (
            str(sample.weapon_box_name[0])
            if sample.weapon_box_name
            else "测试收藏品"
        )
        mid = (float(sample.min_float) + float(sample.max_float)) / 2.0
        recipe = {
            "cost": 10.0,
            "rate": 0.1,
            "substrates_display": [
                {
                    "name": full_name,
                    "float_value": mid,
                    "price": 5.0,
                    "weapon_box": box,
                }
            ],
        }
        payload = saved_recipe_to_bridge_payload(recipe, title="单测配方")
        self.assertEqual(payload["collection_name"], "单测配方")
        self.assertEqual(len(payload["inputs"]), 1)
        self.assertTrue(payload["inputs"][0].get("wear"))

        with patch(
            "core.recipe_bridge.fetch_material_alternatives",
            return_value=[
                {
                    "name": f"{full_name} 备选",
                    "equiv_float": mid,
                    "float_range": "0.1 ~ 0.2",
                    "unit_price_cny": 4.0,
                    "supports_wear": True,
                }
            ],
        ):
            attached = attach_recipe_alternatives(dict(payload), access_token="")
        alts = attached["_alternatives_by_input"][0]
        self.assertEqual(len(alts), 1)
        self.assertTrue(alts[0].get("is_alternative"))

    def test_product_price_payload_builds_expected_map(self) -> None:
        raw = {
            "ordinary": {
                "1": {
                    "MilSpec": {
                        "0.1": {"100": 12.5},
                    }
                }
            }
        }
        price_map = build_price_map(raw)
        self.assertEqual(price_map["ordinary"][1]["MilSpec"][0.1]["100"], 12.5)

    def test_price_lookup_caches_follow_price_map_identity(self) -> None:
        first = {
            "ordinary": {
                7: {
                    "Restricted": {
                        0.1: {"100": 10.0, "101": 20.0},
                        0.2: {"100": 30.0, "101": 40.0},
                    }
                }
            }
        }
        first_expectation = get_expectation_map(first, 7, "受限", False)
        self.assertIs(
            first_expectation,
            get_expectation_map(first, 7, "受限", False),
        )
        first_leaves = first["ordinary"][7]["Restricted"]
        first_nfvs = _sorted_product_nfvs(
            first,
            7,
            "受限",
            False,
            first_leaves,
        )
        self.assertIs(
            first_nfvs,
            _sorted_product_nfvs(first, 7, "受限", False, first_leaves),
        )
        self.assertEqual(first_nfvs, [0.1, 0.2])
        self.assertEqual(
            lookup_pid_price_at_nfv(first, 7, "受限", False, 0.15, "100"),
            30.0,
        )

        second = {
            "ordinary": {
                7: {
                    "Restricted": {
                        0.3: {"100": 99.0},
                    }
                }
            }
        }
        second_expectation = get_expectation_map(second, 7, "受限", False)
        self.assertIsNot(first_expectation, second_expectation)
        self.assertEqual(second_expectation, {0.3: 99.0})
        self.assertEqual(
            lookup_pid_price_at_nfv(second, 7, "受限", False, 0.15, "100"),
            99.0,
        )

    def test_missing_substrate_prices_are_backfilled_after_bundle_load(self) -> None:
        rows, template = self._tradeup_rows("军规级", False, 2)
        rows[0]["price"] = 0
        rows[1]["price"] = 7.5
        box_id = template.weapon_box_id[0]
        price_map = {
            "ordinary": {
                box_id: {
                    "MilSpec": {
                        0.5: {str(template.paint_index): 12.5},
                    }
                }
            }
        }

        repriced, updated, unresolved = backfill_missing_substrate_prices(
            rows,
            price_map,
        )

        self.assertEqual(updated, 1)
        self.assertEqual(unresolved, 0)
        self.assertEqual(repriced[0]["price"], 12.5)
        self.assertEqual(repriced[1]["price"], 7.5)

    def test_custom_price_uses_selected_weapon_box_and_backfill_keeps_it(self) -> None:
        rows, template = self._tradeup_rows("军规级", False, 1)
        primary_box_id = int(template.weapon_box_id[0])
        selected_box_id = primary_box_id + 987654
        price_map = {
            "ordinary": {
                primary_box_id: {
                    "MilSpec": {0.5: {str(template.paint_index): 1.0}}
                },
                selected_box_id: {
                    "MilSpec": {0.5: {str(template.paint_index): 88.0}}
                },
            }
        }
        float_value = float(rows[0]["float_value"])
        self.assertEqual(
            lookup_template_price_value(
                template,
                float_value,
                price_map,
                weapon_box_id=selected_box_id,
            ),
            88.0,
        )
        rows[0]["price"] = 0
        rows[0]["weapon_box_id"] = selected_box_id
        repriced, updated, unresolved = backfill_missing_substrate_prices(rows, price_map)
        self.assertEqual((updated, unresolved), (1, 0))
        self.assertEqual(repriced[0]["price"], 88.0)

    def test_zero_price_substrates_are_not_calculation_candidates(self) -> None:
        rows, _template = self._tradeup_rows("军规级", False, 10)
        for row in rows:
            row["price"] = 0
        self.assertEqual(
            partition_selected_data_by_tradeup_group(rows, eligible_only=True),
            [],
        )

    def test_local_spot_snapshot_syncs_once_per_snapshot_version(self) -> None:
        template = next(
            value
            for value in get_name_map().values()
            if value.weapon_box_id and value.steam
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "spot.sqlite"
            output = root / "product_price_all.json"
            with closing(sqlite3.connect(snapshot)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE prices (
                        market_hash_name TEXT PRIMARY KEY,
                        price_cny REAL NOT NULL
                    );
                    CREATE TABLE bucket_min_prices (
                        market_hash_name TEXT NOT NULL,
                        bucket_lo REAL NOT NULL,
                        bucket_hi REAL NOT NULL,
                        price_cny REAL NOT NULL
                    );
                    CREATE TABLE snapshot_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                names = {str(name) for name in template.steam.values() if name}
                connection.executemany(
                    "INSERT INTO prices VALUES (?, ?)",
                    [(name, 12.5) for name in names],
                )
                connection.executemany(
                    "INSERT INTO bucket_min_prices VALUES (?, ?, ?, ?)",
                    [(name, 0.0, template.max_float, 12.5) for name in names],
                )
                connection.executemany(
                    "INSERT INTO snapshot_meta VALUES (?, ?)",
                    (("synced_at", "test-v1"), ("item_count", str(len(names)))),
                )
                connection.commit()

            payload, changed = sync_product_price_cache(snapshot, output)
            unchanged_payload, changed_again = sync_product_price_cache(
                snapshot,
                output,
                payload,
            )

            mode = "stat_trak" if template.stat_trak else "ordinary"
            self.assertTrue(changed)
            self.assertFalse(changed_again)
            self.assertIs(unchanged_payload, payload)
            self.assertEqual(payload["snapshot_synced_at"], "test-v1")
            self.assertIn(str(template.weapon_box_id[0]), payload[mode])

    def test_tradeup_groups_split_levels_and_silently_drop_short_groups(self) -> None:
        short_covert, _ = self._tradeup_rows("隐秘", False, 4)
        milspec, _ = self._tradeup_rows("军规级", False, 10)
        stattrak_milspec, _ = self._tradeup_rows("军规级", True, 10)

        groups = partition_selected_data_by_tradeup_group(
            [*short_covert, *milspec, *stattrak_milspec],
            eligible_only=True,
        )

        self.assertEqual(
            [
                (quality, stat_trak, k, len(rows))
                for quality, stat_trak, k, rows in groups
            ],
            [
                ("军规级", False, 10, 10),
                ("军规级", True, 10, 10),
            ],
        )

    def test_special_wear_uses_only_group_that_can_make_target(self) -> None:
        covert, covert_template = self._tradeup_rows("隐秘", False, 5)
        milspec, _ = self._tradeup_rows("军规级", False, 10)
        target_pid = str(covert_template.upper_skins[0])

        selected = eligible_selected_data_for_target(
            [*milspec, *covert],
            target_pid,
        )

        self.assertEqual(len(selected), 5)
        self.assertEqual(
            {row["goods_id"] for row in selected},
            {row["goods_id"] for row in covert},
        )

    def test_special_wear_materials_include_purchase_intervals(self) -> None:
        target = next(
            template
            for template in get_name_map().values()
            if template.lower_skins
        )
        target_float = (target.min_float + target.max_float) / 2
        materials = build_special_wear_materials(
            target,
            target=target_float,
            target_low=target_float - 0.000001,
            target_high=target_float + 0.000001,
            pid_map=get_pid_map(),
        )

        self.assertTrue(materials)
        self.assertTrue(
            all(
                material["min_wear"]
                <= material["wear_value"]
                <= material["max_wear"]
                for material in materials
            )
        )
        self.assertEqual(
            neighboring_purchase_interval(0.164861929, min_float=0, max_float=1),
            (0.11, 0.21),
        )
        self.assertEqual(
            neighboring_purchase_interval(0.11540335, min_float=0, max_float=1),
            (0.1, 0.18),
        )

    def test_recipe_results_keep_material_only_in_highest_expectation(self) -> None:
        def substrate(platform: str, float_value: float) -> dict:
            return {
                "name": "AK-47 | 传承（崭新出厂）",
                "float_value": float_value,
                "platform": platform,
            }

        recipes = [
            {
                "expectation": 80.0,
                "rate": 0.4,
                "cost": 50.0,
                "substrates_display": [substrate("steam", 0.03)],
            },
            {
                "expectation": 90.0,
                "rate": 0.8,
                "cost": 50.0,
                "substrates_display": [substrate("buff", 0.03)],
            },
            {
                "expectation": 100.0,
                "rate": 0.2,
                "cost": 50.0,
                "substrates_display": [substrate("buff", 0.03)],
            },
            {
                "expectation": 70.0,
                "rate": 0.1,
                "cost": 50.0,
                "substrates_display": [substrate("buff", 0.04)],
            },
        ]

        selected = filter_non_overlapping_recipes(recipes)

        self.assertEqual(
            [recipe["expectation"] for recipe in selected],
            [100.0, 80.0, 70.0],
        )

    def test_catalog_matches_cs2th_arabesque_collection(self) -> None:
        template = resolve_inventory_skin_template(
            {
                "market_hash_name": "MAC-10 | Arabesque Mosaic (Minimal Wear)",
                "float": 0.08135221898555756,
            }
        )
        self.assertIsNotNone(template)
        self.assertEqual(template.paint_index, "1454")
        self.assertEqual(template.weapon_box_id, [536])
        self.assertEqual(template.quality, "军规级")


class AuthTests(unittest.TestCase):
    def test_new_entitlement_schema_does_not_treat_other_plan_as_tradeup(self) -> None:
        account = _account_from_payload(
            {
                "id": 9,
                "username": "terminal-only",
                "is_member": True,
                "member_until": 2_000_000_000,
                "subscriptions": {
                    "terminal": {"active": True, "expires_at": 2_000_000_000},
                    "all_access": {"active": False, "expires_at": 0},
                },
                "effective_entitlements": ["terminal"],
            }
        )

        self.assertFalse(account.member)
        self.assertEqual(account.effective_entitlements, ("terminal",))

    def test_tradeup_access_matrix_requires_login_and_correct_entitlement(self) -> None:
        free = Account(user_id="1", username="free")
        tradeup = Account(
            user_id="2",
            username="tradeup",
            member=True,
            effective_entitlements=("tradeup",),
        )
        terminal_only = Account(
            user_id="3",
            username="terminal",
            effective_entitlements=("terminal",),
        )

        self.assertFalse(has_tradeup_access(None))
        self.assertFalse(has_tradeup_access(AuthSession("free", free)))
        self.assertTrue(
            has_tradeup_access(
                AuthSession("beta", free, {"tradeup": True})
            )
        )
        self.assertTrue(has_tradeup_access(AuthSession("member", tradeup)))
        self.assertFalse(
            has_tradeup_access(AuthSession("wrong-product", terminal_only))
        )

    def test_auth_http_headers_are_latin1_safe(self) -> None:
        client = AuthClient(enabled=True)
        user_agent = client._http.headers["User-Agent"]
        self.assertEqual(user_agent, f"CS2TH-Tradeup-Assistant/{__version__}")
        user_agent.encode("latin-1")

    def test_product_price_conditional_request_uses_etag(self) -> None:
        class FakeResponse:
            status_code = 304
            headers = {"ETag": '"price-v2"'}
            text = ""
            ok = True

        with patch(
            "core.alchemy_calc.requests.get",
            return_value=FakeResponse(),
        ) as get:
            payload, etag, not_modified = _fetch_product_price_from_api(
                "session-token",
                '"price-v1"',
            )

        self.assertIsNone(payload)
        self.assertEqual(etag, '"price-v2"')
        self.assertTrue(not_modified)
        self.assertEqual(
            get.call_args.kwargs["headers"]["If-None-Match"],
            '"price-v1"',
        )

    def test_disabled_auth_never_makes_network_request(self) -> None:
        client = AuthClient(enabled=False)
        with self.assertRaises(AuthUnavailableError):
            client.login("demo", "password")

    def test_cs2th_login_contract_and_protected_session(self) -> None:
        class FakeResponse:
            def __init__(
                self,
                status_code: int,
                payload: dict,
                cookies: dict[str, str] | None = None,
            ) -> None:
                self.status_code = status_code
                self._payload = payload
                self.cookies = cookies or {}

            def json(self) -> dict:
                return self._payload

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise requests.HTTPError(response=self)

        class FakeHttp:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.calls: list[tuple] = []

            def post(self, url: str, **kwargs):
                self.calls.append(("POST", url, kwargs))
                if url.endswith("/api/auth/login"):
                    return FakeResponse(
                        200,
                        {
                            "ok": True,
                            "session_token": "super-secret-session-token",
                            "user": {
                                "id": 7,
                                "username": "demo",
                                "is_member": True,
                                "member_until": 2_000_000_000,
                                "free_max_cost": 20,
                            },
                        },
                    )
                return FakeResponse(200, {"ok": True})

            def get(self, url: str, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return FakeResponse(
                    200,
                    {
                        "ok": True,
                        "user": {
                            "id": 7,
                            "username": "demo",
                            "is_member": True,
                            "member_until": 2_000_000_000,
                            "free_max_cost": 20,
                        },
                    },
                )

        with TemporaryDirectory() as directory:
            session_file = Path(directory) / "auth_session.json"
            http = FakeHttp()
            client = AuthClient(
                enabled=True,
                base_url="https://cs2th.cn",
                session_file=session_file,
                http_session=http,  # type: ignore[arg-type]
            )
            session = client.login("demo", "password")
            self.assertEqual(session.access_token, "super-secret-session-token")
            self.assertTrue(session.account.member)
            self.assertEqual(http.headers["X-CS2TH-Client"], "cs2th-tools")
            self.assertEqual(http.calls[0][1], "https://cs2th.cn/api/auth/login")
            self.assertEqual(http.calls[0][2]["json"]["client"], "cs2th-tools")
            self.assertNotIn(
                "super-secret-session-token",
                session_file.read_text(encoding="utf-8"),
            )
            loaded = client.load_local_session()
            self.assertEqual(loaded, session)
            refreshed = client.validate_session(session)
            self.assertEqual(refreshed, session)
            client.logout(session)
            self.assertFalse(session_file.exists())
            self.assertEqual(http.calls[-1][1], "https://cs2th.cn/api/auth/logout")

    def test_deployed_login_cookie_fallback(self) -> None:
        class CookieResponse:
            status_code = 200
            cookies = {"cs2th_session": "cookie-session-token"}

            @staticmethod
            def json() -> dict:
                return {
                    "ok": True,
                    "message": "登录成功",
                    "user": {
                        "id": 8,
                        "username": "cookie-user",
                        "is_member": False,
                        "member_until": 0,
                        "free_max_cost": 20,
                    },
                }

            @staticmethod
            def raise_for_status() -> None:
                return None

        class CookieHttp:
            headers: dict[str, str] = {}
            cookies: dict[str, str] = {}

            @staticmethod
            def post(*_args, **_kwargs):
                return CookieResponse()

        with TemporaryDirectory() as directory:
            client = AuthClient(
                enabled=True,
                base_url="https://cs2th.cn",
                session_file=Path(directory) / "session.json",
                http_session=CookieHttp(),  # type: ignore[arg-type]
            )
            session = client.login("cookie-user", "password")
            self.assertEqual(session.access_token, "cookie-session-token")
            self.assertEqual(session.account.username, "cookie-user")

    def test_cs2th_login_rejection_message(self) -> None:
        class RejectedResponse:
            status_code = 400

            @staticmethod
            def json() -> dict:
                return {"detail": "用户名或密码错误"}

        class RejectedHttp:
            headers: dict[str, str] = {}

            @staticmethod
            def post(*_args, **_kwargs):
                return RejectedResponse()

        client = AuthClient(
            enabled=True,
            base_url="https://cs2th.cn",
            http_session=RejectedHttp(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(AuthRejectedError, "用户名或密码错误"):
            client.login("demo", "bad-password")


class InventoryParserTests(unittest.TestCase):
    def test_inventory_total_sums_all_valid_snapshot_prices(self) -> None:
        total, matched = _inventory_total_value(
            [
                {"buff_price": 1.25},
                {"buff_price": "2.75"},
                {"buff_price": None},
                {"buff_price": float("nan")},
            ]
        )
        self.assertEqual(total, 4.0)
        self.assertEqual(matched, 2)

    def test_simulation_groups_sort_by_highest_product_price(self) -> None:
        grouped = _group_simulation_rows_by_weapon_box(
            [
                {"weapon_box": "裂空武器箱", "name": "A", "price": 3800},
                {"weapon_box": "毁灭之手收藏品", "name": "B", "price": 8400},
                {"weapon_box": "裂空武器箱", "name": "C", "price": 2700},
            ]
        )
        self.assertEqual(
            [name for name, _rows in grouped],
            ["毁灭之手收藏品", "裂空武器箱"],
        )

    def test_simulation_price_outcome_compares_with_total_cost(self) -> None:
        self.assertEqual(_simulation_price_outcome(2000, 1800), "profit")
        self.assertEqual(_simulation_price_outcome(1600, 1800), "loss")
        self.assertEqual(_simulation_price_outcome(1800, 1800), "neutral")

    def test_inventory_price_snapshot_is_attached_without_mutating_source(self) -> None:
        source = [{"assetid": "1"}, {"assetid": "2"}]
        with patch(
            "core.alchemy_calc.lookup_inventory_item_price_value",
            side_effect=[12.34, None],
        ):
            priced, matched = apply_inventory_buff_prices(
                source,
                {"ordinary": {}},
                "20260801_120000",
            )

        self.assertEqual(matched, 1)
        self.assertEqual(priced[0]["buff_price"], 12.34)
        self.assertIsNone(priced[1]["buff_price"])
        self.assertEqual(priced[0]["buff_price_fetch_time"], "20260801_120000")
        self.assertEqual(priced[0]["buff_price_source"], "CS2TH")
        self.assertNotIn("buff_price", source[0])

    def test_inventory_cooldown_filter_uses_status_bucket_not_duration_text(self) -> None:
        self.assertEqual(
            _inventory_status_category(
                {
                    "marketable": 0,
                    "cooldown_kind": "trade_hold",
                    "cooldown_ends_at": time.time() + 3 * 86400,
                }
            ),
            "冷却中",
        )
        self.assertEqual(
            _inventory_status_category(
                {
                    "marketable": 1,
                    "cooldown_kind": "market_listed",
                }
            ),
            "Steam在售中",
        )

    def test_star_gloves_tagged_ancient_map_to_extraordinary(self) -> None:
        gloves = {
            "market_hash_name": "★ Sport Gloves | Vice (Field-Tested)",
            "market_name": "运动手套（★） | 迈阿密风云 (久经沙场)",
            "rarity": "ancient",
        }
        covert_gun = {
            "market_hash_name": "AWP | Duality (Field-Tested)",
            "rarity": "ancient_weapon",
        }
        self.assertEqual(_inventory_item_quality_cn(gloves), "非凡")
        self.assertEqual(_inventory_item_quality_cn(covert_gun), "隐秘")
        self.assertGreater(
            _inventory_item_quality_rank(gloves),
            _inventory_item_quality_rank(covert_gun),
        )

    def test_process_inventory_extracts_float_and_status(self) -> None:
        payload = {
            "assets": [{"assetid": "1", "classid": "2", "instanceid": "3"}],
            "descriptions": [
                {
                    "classid": "2",
                    "instanceid": "3",
                    "market_hash_name": "AK-47 | Inheritance (Factory New)",
                    "market_name": "AK-47 | Inheritance (Factory New)",
                    "name": "AK-47 | Inheritance",
                    "owner_descriptions": [],
                    "tags": [],
                }
            ],
            "asset_properties": [
                {
                    "assetid": "1",
                    "asset_properties": [
                        {"propertyid": 2, "float_value": 0.03125},
                        {"propertyid": 1, "int_value": 123},
                    ],
                }
            ],
        }
        rows = process_inventory(payload, 1, steam_inventory_context_id=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["float"], 0.03125)
        self.assertEqual(rows[0]["paintseed"], 123)
        self.assertEqual(rows[0]["marketable"], 1)


if __name__ == "__main__":
    unittest.main()
