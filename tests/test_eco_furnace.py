import base64
import copy
import json
import os
import unittest
from decimal import Decimal
from contextlib import ExitStack
from unittest.mock import Mock, patch

import requests
from Crypto.Hash import SHA256
from Crypto.Signature import pkcs1_15
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.eco_furnace import (
    API_BASE, CREATE_PATH, LIST_PATH, PAY_PATH, STOCK_PATH, PUBLISH_STOCK_PATH, EDIT_PRICE_PATH, EcoApiError, EcoCredentials, EcoFurnaceClient,
    build_create_payload, canonical_sign_text, generate_key_pair, load_private_key,
    match_recipe_inventory, save_eco_credentials,
)
from tests.test_purchase_batches import _special_recipe
from ui.dialogs.eco_furnace_dialog import EcoFurnaceDialog


class EcoApiTests(unittest.TestCase):
    def test_furnace_listing_total_uses_actual_exact_assets_not_target_prices(self):
        client = EcoFurnaceClient(EcoCredentials("test", ""))
        rows = {"PageResult": [{"FurnaceID": "f1", "AllMatRefPrice": 999}, {"FurnaceID": "f2"}]}
        binding = {"furnace_id": "f1", "steam_id": "76561198000000001", "asset_ids": ["a", "b"], "material_listing": {"prices": {"a": 100, "b": 100}}}
        with patch.object(client, "list_furnaces", return_value=rows), patch("core.purchase_batches.list_purchase_batches", return_value=[("path", {"recipes": [{}]})]), patch("core.eco_publication.recipe_publication", return_value=binding), patch.object(client, "private_listings", return_value={"a": {"Price": 11.47}, "b": {"Price": 11.55}}) as listings:
            result = client.list_furnaces_with_prices()
            self.assertEqual(result["PageResult"][0]["_listing_total"], 23.02)
            self.assertIsNone(result["PageResult"][1]["_listing_total"])
            listings.assert_called_once_with(binding["steam_id"])
            listings.return_value.pop("b")
            self.assertIsNone(client.list_furnaces_with_prices()["PageResult"][0]["_listing_total"])

    def test_private_material_listing_uses_exact_stock_ids_and_secret_flag(self):
        client = EcoFurnaceClient(EcoCredentials("test", ""))
        assets = [{"AssetId": "10001", "Price": 12.345}]
        with patch.object(client, "private_listings", return_value={"10001": {"Price": 12.35}}), patch.object(client, "stock_items", return_value={"10001": {"StockId": "stock-1", "SteamGameId": "730", "CanPublish": True}}), patch.object(client, "_post", return_value=[{"AssetId": "10001", "IsSuccess": True}]) as post:
            client.publish_private_materials("76561198000000001", assets)
        post.assert_called_once_with(PUBLISH_STOCK_PATH, {"IsSecret": True, "Assets": [{"StockId": "stock-1", "AssetId": "10001", "SteamGameId": "730", "Price": 12.35, "Description": ""}]})

    def test_already_private_listed_material_is_repriced_and_verified(self):
        client = EcoFurnaceClient(EcoCredentials("test", ""))
        old = {"10001": {"GoodsNum": "own-good-1", "Price": 5.47}}
        for actual, success in ((15.47, True), (5.47, None)):
            with self.subTest(actual=actual), patch.object(client, "stock_items", return_value={"10001": {"StockId": "stock-1", "Status": 3, "CanPublish": False}}), patch.object(client, "private_listings", side_effect=[old, {"10001": {"GoodsNum": "own-good-1", "Price": actual}}]), patch.object(client, "_post", return_value=True) as post:
                result = client.publish_private_materials("76561198000000001", [{"AssetId": "10001", "Price": 15.47}])
                post.assert_called_once_with(EDIT_PRICE_PATH, {"goodsBatchEditList": [{"GoodsNum": "own-good-1", "AssetId": "10001", "SteamGameId": "730", "SellingPrice": 15.47, "Description": ""}]})
                self.assertIs(result[0]["IsSuccess"], success)

    def test_private_listing_does_not_resubmit_unpublishable_material(self):
        client = EcoFurnaceClient(EcoCredentials("test", ""))
        with patch.object(client, "stock_items", return_value={"10001": {"StockId": "stock-1", "CanPublish": False}}), patch.object(client, "_post") as post:
            with self.assertRaises(EcoApiError):
                client.publish_private_materials("76561198000000001", [{"AssetId": "10001", "Price": 12}])
        post.assert_not_called()
    @classmethod
    def setUpClass(cls):
        cls.private, cls.public = generate_key_pair()
        cls.creds = EcoCredentials("partner-test", cls.private)

    def assets(self):
        return [{"AssetId": str(10000+i), "Price": 12.345, "Description": "材料"} for i in range(10)]

    def response(self, data=None, code=0):
        response = Mock(status_code=200)
        response.json.return_value = {"ResultCode": code, "ResultData": data, "ResultMsg": "测试响应"}
        return response

    def test_documented_signature_canonicalization_preserves_nested_order(self):
        data = {"Timestamp": "1", "PartnerId": "p", "AssetList": [{"Price": 2.0, "Description": "中文", "AssetId": "a"}], "SelfOpen": False, "Sign": "ignore"}
        self.assertEqual(canonical_sign_text(data), 'AssetList=[{"Price":2.0,"Description":"中文","AssetId":"a"}]&PartnerId=p&SelfOpen=False&Timestamp=1')

    def test_create_uses_official_url_valid_rsa_signature_and_no_redirect(self):
        session = Mock()
        session.post.return_value = self.response()
        client = EcoFurnaceClient(self.creds, session=session)
        payload = build_create_payload("76561198000000001", "测试合炉", self.assets())
        with patch("core.eco_furnace.time.time", return_value=1234):
            client.create(payload)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], API_BASE+CREATE_PATH)
        self.assertFalse(kwargs["allow_redirects"])
        sent = json.loads(kwargs["data"])
        signature = base64.b64decode(sent.pop("Sign"))
        pkcs1_15.new(load_private_key(self.private).public_key()).verify(SHA256.new(canonical_sign_text(sent).encode()), signature)
        self.assertEqual(sent["Timestamp"], "1234")
        self.assertEqual(sent["PublishState"], 101)
        self.assertTrue(sent["SelfOpen"])
        self.assertEqual(sent["AssetList"][0]["Price"], 12.35)
        self.assertNotIn(self.private, kwargs["data"].decode())
        self.assertEqual(payload["AssetList"][0]["AssetId"], "10000")

    def test_service_identity_omits_partner(self):
        creds = EcoCredentials("unused", self.private, "service", "open-user")
        self.assertEqual(creds.identity_params(), {"ServiceProviderId": "service", "OpenID": "open-user"})
        with self.assertRaises(EcoApiError):
            EcoCredentials("p", self.private, "service", "").identity_params()

    def test_private_key_supports_pem_and_base64_but_rejects_public_key(self):
        raw = load_private_key(self.private)
        self.assertEqual(load_private_key(base64.b64encode(raw.export_key(format="DER")).decode()).n, raw.n)
        with self.assertRaises(EcoApiError):
            load_private_key(self.public)

    def test_reject_duplicate_or_missing_assets_and_bad_amounts(self):
        for mutate in (
            lambda a: a[1].update(AssetId=a[0]["AssetId"]),
            lambda a: a[0].update(AssetId=""),
            lambda a: a[0].update(Price=0),
            lambda a: a[0].update(Price=float("nan")),
        ):
            assets = self.assets()
            mutate(assets)
            with self.assertRaises(EcoApiError):
                build_create_payload("76561198000000001", "炉", assets)
        with self.assertRaises(EcoApiError):
            build_create_payload("wrong-account", "炉", self.assets())
        with self.assertRaises(EcoApiError):
            build_create_payload("76561198000000001", "炉", self.assets(), publish_state=0)

    def test_network_timeout_does_not_retry_or_claim_failure_is_definitive(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        with self.assertRaises(EcoApiError) as context:
            EcoFurnaceClient(self.creds, session=session).create({})
        self.assertTrue(context.exception.uncertain)
        self.assertEqual(session.post.call_count, 1)

    def test_rejects_api_errors_malformed_response_and_redirect(self):
        for response in (self.response(code=6003), Mock(status_code=302), self.response()):
            if response.status_code == 200 and response.json.return_value["ResultCode"] == 0:
                response.json.return_value = {"bad": True}
            session = Mock()
            session.post.return_value = response
            with self.assertRaises(EcoApiError):
                EcoFurnaceClient(self.creds, session=session).create({})
            session.post.assert_called_once()

    def test_market_query_is_allowed_through_signed_request(self):
        from core.eco_furnace import MARKET_LIST_PATH
        session = Mock()
        session.post.return_value = self.response({"PageResult": [{"SellingPrice": 60, "PaintWear": "0.17"}]})
        client = EcoFurnaceClient(self.creds, session=session)
        with patch.object(client, "stock_items", return_value={"1": {"HashName": "Test"}}):
            self.assertEqual(client.market_material_prices("76561198000000001", [{"AssetId": "1", "float_value": 0.179}]), {"1": 60})
        self.assertEqual(session.post.call_args.args[0], API_BASE + MARKET_LIST_PATH)
        payload = json.loads(session.post.call_args.kwargs["data"])
        self.assertIn("Sign", payload)
        self.assertEqual(payload["HashName"], "Test")

    def test_market_quotes_filter_wear_and_reuse_same_window(self):
        client = EcoFurnaceClient(self.creds)
        assets = [{"AssetId": "1", "float_value": 0.179}, {"AssetId": "2", "float_value": 0.17}]
        with patch.object(client, "stock_items", return_value={"1": {"HashName": "Test"}, "2": {"HashName": "Test"}}), patch.object(client, "_post", return_value={"PageResult": [
            {"SellingPrice": 60, "PaintWear": "0.17"},
            {"SellingPrice": 1, "PaintWear": "0.20"},
            {"SellingPrice": 65, "PaintWear": "0.16"},
        ]}) as query:
            self.assertEqual(client.market_material_prices("76561198000000001", assets), {"1": 60, "2": 60})
            self.assertEqual(query.call_count, 1)
            body = query.call_args.args[1]
            self.assertEqual((body["StartPaintWear"], body["EndPaintWear"]), (0.15, 0.18))

    def test_eco_reference_prices_use_platform_price_and_paginate_requested_account(self):
        session = Mock()
        session.post.side_effect = [
            self.response({"TotalRecord": 101, "PageResult": [{"AssetId": "10001", "SteamId": "76561198000000001", "Price": 12.345, "BasePrice": 1, "SteamPrice": 99}]}),
            self.response({"TotalRecord": 101, "PageResult": [{"AssetId": "10002", "Price": 2.4}]}),
        ]
        with patch("core.eco_furnace.time.sleep"):
            prices = EcoFurnaceClient(self.creds, session=session).reference_prices("76561198000000001", ["10001", "10002"])
        self.assertEqual(prices, {"10001": 12.35, "10002": 2.4})
        self.assertEqual(session.post.call_count, 2)
        for page, call in enumerate(session.post.call_args_list, 1):
            self.assertEqual(call.args[0], API_BASE+STOCK_PATH)
            payload = json.loads(call.kwargs["data"])
            self.assertEqual(payload["SteamId"], "76561198000000001")
            self.assertEqual(payload["PageIndex"], page)

    def test_eco_reference_prices_do_not_fill_missing_quotes_from_cost_or_other_accounts(self):
        session = Mock()
        session.post.return_value = self.response({"TotalRecord": 3, "PageResult": [
            {"AssetId": "1", "SteamId": "wrong-account", "Price": 5},
            {"AssetId": "2", "Price": None, "BasePrice": 5},
            {"AssetId": "3", "Price": "NaN"},
        ]})
        prices = EcoFurnaceClient(self.creds, session=session).reference_prices("76561198000000001", ["1", "2", "3"])
        self.assertEqual(prices, {})

    def test_query_uses_documented_state_and_pagination(self):
        session = Mock()
        session.post.return_value = self.response({"PageResult": [], "TotalRecord": 0})
        result = EcoFurnaceClient(self.creds, session=session).list_furnaces(200, 3)
        self.assertEqual(result["PageResult"], [])
        args, kwargs = session.post.call_args
        sent = json.loads(kwargs["data"])
        self.assertEqual(args[0], API_BASE+LIST_PATH)
        self.assertEqual((sent["HL_FurnaceState"], sent["PageIndex"], sent["PageSize"]), (200, 3, 20))

    def test_payment_requires_true_confirmation_from_server(self):
        session = Mock()
        session.post.return_value = self.response(False)
        with self.assertRaises(EcoApiError):
            EcoFurnaceClient(self.creds, session=session).pay("furnace-test")
        self.assertEqual(session.post.call_args.args[0], API_BASE+PAY_PATH)
        sent = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(sent["FurnaceID"], "furnace-test")

    def test_saved_credentials_use_protected_private_key(self):
        with patch("core.eco_furnace._protect_token", return_value="dpapi:protected") as protect, patch("core.eco_furnace.update_app_settings", return_value=True) as update:
            save_eco_credentials(self.creds)
        protect.assert_called_once_with(self.private)
        serialized = json.dumps(update.call_args.kwargs)
        self.assertNotIn(self.private, serialized)
        self.assertIn("dpapi:protected", serialized)
        self.assertNotIn(self.private, repr(self.creds))

    def test_inventory_matching_never_reuses_assets_or_substitutes_explicit_id(self):
        with patch("core.eco_furnace.recipe_substrate_inventory_key", return_value="skin-wear"), patch("core.eco_furnace.inventory_item_match_key", return_value="skin-wear"):
            rows = match_recipe_inventory({"substrates_display": [{"name": "a"}, {"name": "a"}]}, [{"assetid": "1"}])
            self.assertEqual([row["AssetId"] for row in rows], ["1", ""])
            rows = match_recipe_inventory({"substrates_display": [{"name": "a", "steam_assetid": "2"}]}, [{"assetid": "1"}])
            self.assertEqual(rows[0]["AssetId"], "")


class EcoDialogTests(unittest.TestCase):
    def test_new_furnace_is_auto_selected_among_identically_named_old_furnaces(self):
        self.assertFalse(self.dialog.markup_spin.isEnabled())
        self.assertFalse(self.dialog.markup_button.isEnabled())
        self.assertFalse(self.dialog.material_table.cellWidget(0, self.dialog.PRICE_COLUMN).isEnabled())
        old = {"FurnaceID": "old", "Title": "正期望MK", "AllMatRefPrice": 400, "AddMatCount": 0}
        new = {"FurnaceID": "new", "Title": "正期望MK", "AllMatRefPrice": 486.44, "AddMatCount": 0}
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.list_all_furnaces.side_effect = [[old], [new, old]]
            client.return_value.create.return_value = None
            self.dialog._create()
            self.finish()
            client.return_value.create.assert_called_once()
            client.return_value.pay.assert_not_called()
        self.assertEqual(self.dialog._created_furnace_id, "new")
        self.assertTrue(self.dialog.markup_spin.isEnabled())
        self.assertTrue(self.dialog.markup_button.isEnabled())
        self.assertTrue(self.dialog.material_table.cellWidget(0, self.dialog.PRICE_COLUMN).isEnabled())
        self.assertTrue(self.dialog.create_button.isHidden())
        self.assertFalse(self.dialog.private_listing_button.isHidden())
        self.assertEqual(self.dialog.workflow_furnace_combo.currentData()["FurnaceID"], "new")
        self.assertIn("¥486.44", self.dialog.reference_total_label.text())
        self.assertNotIn("new", self.dialog.workflow_furnace_combo.currentText())

    def test_successful_create_with_failed_lookup_does_not_allow_recreation(self):
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.list_all_furnaces.side_effect = [[], EcoApiError("read failed")]
            client.return_value.create.return_value = None
            self.dialog._create()
            self.finish()
            self.dialog._create()
            client.return_value.create.assert_called_once()
        self.assertTrue(self.dialog._create_locked)
        self.assertTrue(self.dialog._awaiting_new_furnace)

    def test_default_name_account_and_purchase_cost_are_independent_of_sale_prices(self):
        self.assertEqual(self.dialog.title_edit.text(), "正期望MK")
        self.assertEqual(self.dialog.account_combo.currentText(), "Test")
        self.assertEqual(self.dialog.purchase_cost_label.text(), "收料成本：¥100.00")
        self.dialog.material_table.cellWidget(0, self.dialog.PRICE_COLUMN).setValue(99)
        self.assertEqual(self.dialog.purchase_cost_label.text(), "收料成本：¥100.00")
        self.assertIn("¥189.00", self.dialog.sale_total_label.text())
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.private, _ = generate_key_pair()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.recipe = _special_recipe()
        self.inventory = [{"assetid": str(10000+i), "market_name": row["name"], "float": row["float_value"]} for i, row in enumerate(self.recipe["substrates_display"])]
        self.stack.enter_context(patch("ui.dialogs.eco_furnace_dialog.list_profile_entries", return_value=[{"id": "test", "display_name": "Test"}]))
        self.stack.enter_context(patch("ui.dialogs.eco_furnace_dialog.load_steam_account_config_dict", return_value={"steam_id": "76561198000000001"}))
        self.stack.enter_context(patch("ui.dialogs.eco_furnace_dialog.load_profile_inventory_items", return_value=self.inventory))
        self.stack.enter_context(patch("ui.dialogs.eco_furnace_dialog.load_eco_credentials", return_value=EcoCredentials("partner-test", self.private)))
        self.dialog = EcoFurnaceDialog(self.recipe)
        self.addCleanup(self.cleanup_dialog)

    def cleanup_dialog(self):
        if self.dialog._worker:
            self.dialog._worker.wait(3000)
            self.app.processEvents()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def finish(self):
        worker = self.dialog._worker
        self.assertIsNotNone(worker)
        self.assertTrue(worker.wait(3000))
        self.app.processEvents()
        self.assertIsNone(self.dialog._worker)

    def test_defaults_public_ten_percent_auto_open_and_all_inventory_materials_mapped(self):
        self.assertEqual(self.dialog.publish_combo.currentData(), 101)
        self.assertTrue(self.dialog.self_open_check.isChecked())
        self.assertEqual(self.dialog.fee_spin.value(), 10)
        self.assertTrue(self.dialog.free_fee_check.isChecked())
        self.assertTrue(self.dialog.create_button.isEnabled())
        self.assertEqual(len(set(row["AssetId"] for row in self.dialog._assets)), 10)

    def test_personal_mode_matches_eco_identity_label_and_hides_service_fields(self):
        self.assertEqual(self.dialog.credential_mode_combo.currentData(), "personal")
        self.assertIn("身份 ID", self.dialog._settings_form.labelForField(self.dialog.partner_edit).text())
        self.assertTrue(self.dialog.service_edit.isHidden())
        self.assertTrue(self.dialog.open_id_edit.isHidden())
        self.dialog.service_edit.setText("unused-service")
        self.dialog.open_id_edit.setText("unused-user")
        self.assertEqual(self.dialog._credentials().identity_params(), {"PartnerId": "partner-test"})

    def test_service_mode_does_not_send_hidden_personal_identity(self):
        self.dialog.credential_mode_combo.setCurrentIndex(1)
        self.dialog.service_edit.setText("service")
        self.dialog.open_id_edit.setText("authorized-user")
        self.assertTrue(self.dialog.partner_edit.isHidden())
        self.assertFalse(self.dialog.service_edit.isHidden())
        self.assertEqual(self.dialog._credentials().identity_params(), {"ServiceProviderId": "service", "OpenID": "authorized-user"})
        self.dialog.credential_mode_combo.setCurrentIndex(0)
        self.assertEqual(self.dialog._credentials().identity_params(), {"PartnerId": "partner-test"})

    def test_key_generation_fills_private_key_and_explains_public_key_binding(self):
        from core.eco_furnace import public_key_for
        public = public_key_for(self.private)
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch(
            "ui.dialogs.eco_furnace_dialog.generate_key_pair", return_value=(self.private, public)
        ):
            self.dialog._generate_key()
        self.assertEqual(self.dialog.key_edit.text(), self.private)
        self.assertEqual(self.dialog.public_edit.text(), public)
        self.assertIn("签名验证", self.dialog.status_label.text())
        self.assertIn("身份 ID", self.dialog.status_label.text())

    def test_price_totals_change_live_without_changing_reference_value(self):
        first_price = self.dialog.material_table.cellWidget(0, self.dialog.PRICE_COLUMN)
        first_price.setValue(12.34)
        expected = sum(self.dialog.material_table.cellWidget(i, self.dialog.PRICE_COLUMN).value() for i in range(10))
        self.assertIn(f"¥{expected:.2f}", self.dialog.sale_total_label.text())
        self.assertIn("创建后查询", self.dialog.reference_total_label.text())
        self.dialog._created_furnace_id = "created-furnace"
        self.dialog._show_furnaces({"PageResult": [{"FurnaceID": "unrelated", "AllMatRefPrice": 999}, {"FurnaceID": "created-furnace", "AllMatRefPrice": 25}]})
        self.assertEqual(self.dialog.furnace_list.item(1).data(256)["reference"], "¥25.00")
        self.assertIn("¥25.00", self.dialog.reference_total_label.text())
        self.assertEqual(first_price.value(), 12.34)
        first_price.setValue(13.34)
        self.assertIn("¥25.00", self.dialog.reference_total_label.text())
        self.assertIn(f"¥{expected+1:.2f}", self.dialog.sale_total_label.text())

    def test_bulk_markup_uses_market_percentage_without_compounding(self):
        self.dialog._market_prices = {asset["AssetId"]: 60 for asset in self.dialog._assets}
        self.dialog.markup_spin.setValue(110)
        self.dialog._increase_prices()
        prices = [self.dialog.material_table.cellWidget(i, self.dialog.PRICE_COLUMN) for i in range(10)]
        self.assertEqual([price.value() for price in prices], [66] * 10)
        self.assertIn("660.00", self.dialog.sale_total_label.text())
        self.dialog._increase_prices()
        self.assertEqual([price.value() for price in prices], [66] * 10)

    def test_missing_market_quote_does_not_use_purchase_cost(self):
        prices = [self.dialog.material_table.cellWidget(i, self.dialog.PRICE_COLUMN) for i in range(10)]
        self.assertEqual([price.value() for price in prices], [0] * 10)
        self.dialog._increase_prices()
        self.assertEqual([price.value() for price in prices], [0] * 10)
        self.assertIn("读取全部材料", self.dialog.status_label.text())

    def test_bulk_markup_overflow_leaves_all_prices_unchanged(self):
        prices = [self.dialog.material_table.cellWidget(i, self.dialog.PRICE_COLUMN) for i in range(10)]
        self.dialog._market_prices = {asset["AssetId"]: prices[0].maximum() for asset in self.dialog._assets}
        before = [price.value() for price in prices]
        self.dialog._increase_prices()
        self.assertEqual([price.value() for price in prices], before)
        self.assertIn("本次未修改", self.dialog.status_label.text())

    def test_furnace_reference_value_uses_dedicated_field_without_fallback(self):
        self.dialog._show_furnaces({"PageResult": [
            {"FurnaceID": "one", "AllMatRefPrice": "533.8", "Price": 333.8},
            {"FurnaceID": "two", "Price": 333.8, "RealPrice": 384.19},
            {"FurnaceID": "three", "AllMatRefPrice": 0},
            {"FurnaceID": "four", "AllMatRefPrice": "NaN"},
        ]})
        self.assertEqual([self.dialog.furnace_list.item(i).data(256)["reference"] for i in range(4)], ["¥533.80", "未返回", "¥0.00", "未返回"])

    def test_ten_materials_fit_without_scroll_or_large_gap_under_form(self):
        from ui.theme import build_stylesheet
        old_style = self.app.styleSheet()
        try:
            self.app.setStyleSheet(build_stylesheet("light"))
            with patch("ui.dialogs.eco_furnace_dialog.apply_frameless_modal_geometry"):
                self.dialog.resize(1040, 860)
                self.dialog.show()
                self.app.processEvents()
                table = self.dialog.material_table
                last = table.visualItemRect(table.item(9, 0))
                self.assertLessEqual(last.bottom(), table.viewport().height())
                self.assertEqual(table.verticalScrollBar().maximum(), 0)
                form = self.dialog._publish_form_widget
                self.assertLess(table.y() - (form.y()+form.height()), 20)
                self.assertLessEqual(table.cellWidget(0, self.dialog.PRICE_COLUMN).height(), table.rowHeight(0))
        finally:
            self.dialog.hide()
            self.app.setStyleSheet(old_style)

    def test_create_calls_only_create_and_prevents_second_submission(self):
        publications = []
        self.dialog.publication_changed.connect(lambda *args: publications.append(args))
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.create.return_value = None
            self.dialog.create_button.click()
            self.finish()
            self.dialog.create_button.click()
            client.return_value.create.assert_called_once()
            client.return_value.pay.assert_not_called()
            self.assertTrue(self.dialog._create_locked)
            self.assertFalse(self.dialog.material_table.isHidden())
            self.assertIs(self.dialog.tabs.currentWidget(), self.dialog._publish_tab)
            self.assertFalse(hasattr(self.dialog, "pay_button"))
            payload = client.return_value.create.call_args.args[0]
            self.assertEqual(payload["ReceiveSteamID"], "76561198000000001")
            self.assertEqual(len(payload["AssetList"]), 10)
            self.assertEqual([row[0] for row in publications], ["pending", "submitted"])
            self.assertEqual(publications[0][1]["ReceiveSteamID"], payload["ReceiveSteamID"])
        self.assertEqual(self.recipe, _special_recipe())

    def test_private_listing_partial_success_retries_only_failed_assets(self):
        self.dialog._create_locked = True
        self.dialog._sync_publish_view()
        self.dialog._show_furnaces({"PageResult": [{"FurnaceID": "f1", "Title": self.dialog.title_edit.text(), "AllMatRefPrice": 486.44, "AddMatCount": 0, "CanPayment": False}]})
        self.dialog.workflow_furnace_combo.setCurrentIndex(1)
        self.assertIn("486.44", self.dialog.reference_total_label.text())
        ids = [row["AssetId"] for row in self.dialog._assets]
        results = [{"AssetId": asset_id, "IsSuccess": index != 9, "ErrorMsg": "try later"} for index, asset_id in enumerate(ids)]
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.publish_private_materials.return_value = results
            client.return_value.list_furnaces_with_prices.return_value = {"PageResult": [{"FurnaceID": "f1", "Title": "正期望MK", "AllMatRefPrice": 486.44, "AddMatCount": 10, "CanPayment": True}]}
            self.dialog._publish_materials()
            self.finish()
            self.assertFalse(self.dialog.material_table.isHidden())
            self.assertIs(self.dialog.tabs.currentWidget(), self.dialog._publish_tab)
            client.return_value.list_furnaces_with_prices.assert_not_called()
            self.assertEqual(len(self.dialog._published_asset_ids), 9)
            client.return_value.publish_private_materials.return_value = [{"AssetId": ids[-1], "IsSuccess": True}]
            self.dialog._publish_materials()
            self.assertTrue(self.dialog._worker.wait(3000))
            self.app.processEvents()
            self.assertIs(self.dialog.tabs.currentWidget(), self.dialog._list_tab)
            self.finish()
            client.return_value.list_furnaces_with_prices.assert_called_once_with(100, 1)
            self.assertEqual(self.dialog.furnace_list.currentRow(), 0)
            self.assertIn("请自行进入ECO APP缴纳押金", self.dialog.status_label.text())
            self.assertEqual(len(client.return_value.publish_private_materials.call_args.args[1]), 1)
            client.return_value.pay.assert_not_called()
        self.assertEqual(len(self.dialog._published_asset_ids), len(self.dialog._assets))
        self.assertFalse(hasattr(self.dialog, "pay_button"))

    def test_uncertain_private_listing_requires_platform_check(self):
        self.dialog._create_locked = True
        self.dialog._sync_publish_view()
        self.dialog._workflow_furnace = {"FurnaceID": "f1", "Title": "test", "CanPayment": False}
        with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.publish_private_materials.side_effect = EcoApiError("timeout", uncertain=True)
            self.dialog._publish_materials()
            self.finish()
        self.assertTrue(self.dialog._listing_uncertain)
        self.assertFalse(self.dialog.private_listing_button.isEnabled())

    def test_stock_status_recovers_unknown_listing_without_resubmitting(self):
        self.dialog._listing_uncertain = True
        ids = [row["AssetId"] for row in self.dialog._assets]
        stock = {asset_id: {"Status": 3 if index < 9 else 1, "CanPublish": index == 9} for index, asset_id in enumerate(ids)}
        with patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            client.return_value.stock_items.return_value = stock
            self.dialog._check_stock_status()
            self.finish()
            client.return_value.publish_private_materials.assert_not_called()
        self.assertFalse(self.dialog._listing_uncertain)
        self.assertEqual(len(self.dialog._published_asset_ids), 9)

    def test_existing_furnace_resumes_material_stage_with_listing_progress(self):
        dialog = EcoFurnaceDialog(self.recipe, furnace_id="existing-f1", resume=True,
                                  listing_state={"listed_assets": ["10000"], "uncertain": True})
        try:
            self.assertFalse(dialog.material_table.isHidden())
            self.assertTrue(dialog._create_locked)
            self.assertEqual(dialog._created_furnace_id, "existing-f1")
            self.assertEqual(dialog._published_asset_ids, {"10000"})
            self.assertFalse(dialog.private_listing_button.isEnabled())
        finally:
            dialog.deleteLater()

    def test_single_page_applies_existing_furnace_without_creating_again(self):
        self.assertFalse(self.dialog.create_button.isHidden())
        self.assertTrue(self.dialog.private_listing_button.isHidden())
        self.dialog._show_furnaces({"PageResult": [{"FurnaceID": "existing-1", "Title": "已经创建的合炉", "AllMatRefPrice": 486.44, "AddMatCount": 0, "CanPayment": False}]})
        with patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
            self.dialog.workflow_furnace_combo.setCurrentIndex(1)
            client.assert_not_called()
        self.assertEqual(self.dialog._created_furnace_id, "existing-1")
        self.assertTrue(self.dialog.markup_spin.isEnabled())
        self.assertTrue(self.dialog.markup_button.isEnabled())
        self.assertFalse(self.dialog.private_listing_button.isHidden())
        self.assertIn("¥486.44", self.dialog.reference_total_label.text())
        self.assertTrue(self.dialog.create_button.isHidden())
        self.assertFalse(self.dialog.material_table.isHidden())
        self.assertTrue(self.dialog.material_table.cellWidget(0, self.dialog.PRICE_COLUMN).isEnabled())
        self.assertTrue(self.dialog.private_listing_button.isEnabled())

    def test_create_failure_emits_uncertain_or_unlisted_without_retry(self):
        for uncertain, expected in ((True, "uncertain"), (False, "unlisted")):
            self.dialog._create_locked = False
            publications = []
            callback = lambda *args: publications.append(args)
            self.dialog.publication_changed.connect(callback)
            with patch("ui.dialogs.eco_furnace_dialog.ask_confirmation", return_value=True), patch("ui.dialogs.eco_furnace_dialog.EcoFurnaceClient") as client:
                client.return_value.create.side_effect = EcoApiError("failed", uncertain=uncertain)
                self.dialog._create()
                self.finish()
                client.return_value.create.assert_called_once()
            self.assertEqual([row[0] for row in publications], ["pending", expected])
            self.dialog.publication_changed.disconnect(callback)

    def test_uncertain_creation_stays_locked_and_tells_user_to_query(self):
        self.dialog._start_request("create", lambda: (_ for _ in ()).throw(EcoApiError("请查询结果", uncertain=True)))
        self.finish()
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertIn("查询", self.dialog.status_label.text())

    def test_payment_is_unavailable_even_for_payable_furnaces(self):
        for payable in (False, True):
            self.dialog._show_furnaces({"PageResult": [{"FurnaceID": "id", "Title": "test", "CanPayment": payable}]})
            self.dialog.furnace_list.setCurrentRow(0)
            self.assertFalse(hasattr(self.dialog, "pay_button"))
            self.assertFalse(hasattr(self.dialog, "_pay_furnace"))
            self.assertEqual(self.dialog.deposit_hint_label.text(), "请自行进入ECO APP缴纳押金")

    def test_close_does_not_destroy_active_request(self):
        self.dialog._worker = Mock()
        with patch("PySide6.QtWidgets.QDialog.reject") as reject:
            self.dialog.reject()
            reject.assert_not_called()
        self.dialog._worker = None


if __name__ == "__main__":
    unittest.main()
