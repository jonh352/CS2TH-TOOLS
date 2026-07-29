from __future__ import annotations

import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from unittest.mock import patch

from core.alchemy_quality import (
    get_name_map,
    get_pid_map,
    resolve_inventory_skin_template,
)
from core.auth_client import AuthClient, AuthRejectedError, AuthUnavailableError
from core.float32_wear_prefix import find_float32_range_intersection
from core.alchemy_calc import (
    _fetch_product_price_from_api,
    backfill_missing_substrate_prices,
    build_price_map,
    eligible_selected_data_for_target,
    filter_non_overlapping_recipes,
    partition_selected_data_by_tradeup_group,
)
from core.data_utils import SkinTemplate
from core.platform_links import links_for_recipe_material, links_for_template
from core.market_candidates import (
    clear_provider_auth,
    fetch_buff_candidates,
    fetch_youpin_candidates,
    provider_auth_available,
    save_buff_auth,
    save_youpin_auth,
    validate_provider_login,
)
from core.product_price_sync import sync_product_price_cache
from core.recipe_bridge import (
    cs2th_detail_to_saved_recipe,
    material_wear_range,
    parse_recipe_reference,
)
from core.special_wear_names import get_skin_full_names_without_appearance
from core.special_wear_materials import (
    build_special_wear_materials,
    neighboring_purchase_interval,
)
from core.steam.inventory_pipeline import process_inventory
from ui.pages.inventory import (
    _inventory_item_quality_cn,
    _inventory_item_quality_rank,
    _inventory_status_category,
)
from ui.workers.market_login import _tokens_from_storage


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

    def test_marketplace_links_use_template_ids(self) -> None:
        template = get_name_map()["AK-47 | 传承"]
        links = links_for_template(template, "崭新出厂")
        self.assertIn("buff.163.com/goods/", links["buff"])
        self.assertIn("steamcommunity.com/market/listings/730/", links["steam"])
        self.assertEqual(set(links), {"buff", "yyyp", "c5", "eco", "steam"})

    def test_exact_wear_candidate_parsers(self) -> None:
        template = get_name_map()["USP消音版 | 破颚者"]

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        with patch(
            "core.market_candidates.requests.get",
            return_value=FakeResponse(
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
            ),
        ):
            buff_rows = fetch_buff_candidates(
                template=template,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
            )
        self.assertEqual(len(buff_rows), 1)
        self.assertEqual(buff_rows[0]["platform"], "buff")
        self.assertAlmostEqual(buff_rows[0]["float_value"], 0.164862)

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
        ):
            youpin_rows = fetch_youpin_candidates(
                template=template,
                display_name="USP消音版 | 破颚者",
                min_wear=0.15,
                max_wear=0.18,
                max_pages=1,
                request_interval=1,
            )
        self.assertEqual(len(youpin_rows), 1)
        self.assertEqual(youpin_rows[0]["platform"], "yyyp")
        self.assertAlmostEqual(youpin_rows[0]["price"], 19.9)

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

    def test_buff_login_validation_rejects_browser_only_hint(self) -> None:
        with patch("core.market_candidates._buff_cookie", return_value=""):
            result = validate_provider_login("buff")

        self.assertFalse(result["ok"])
        self.assertFalse(result["indeterminate"])
        self.assertIn("APP 未获取", result["message"])

    def test_youpin_login_validation_returns_account_name(self) -> None:
        class FakeResponse:
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
                "core.market_candidates.requests.get",
                return_value=FakeResponse(),
            ) as get,
        ):
            result = validate_provider_login("yyyp")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_name"], "测试用户")
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer valid-token",
        )

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

    def test_youpin_login_capture_extracts_only_token_fields(self) -> None:
        tokens = _tokens_from_storage(
            {
                "local": {
                    "themePreference": "this-is-not-a-login-token-value",
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
    def test_auth_http_headers_are_latin1_safe(self) -> None:
        client = AuthClient(enabled=True)
        user_agent = client._http.headers["User-Agent"]
        self.assertEqual(user_agent, "CS2TH-Tradeup-Assistant/0.3.0")
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
