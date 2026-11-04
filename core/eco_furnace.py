"""ECO 官方开放平台合炉接口。

协议来源：
https://s.apifox.cn/8dd30eef-e628-4390-b503-846412c608ee/api-442268835
https://s.apifox.cn/8dd30eef-e628-4390-b503-846412c608ee/doc-5242863
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests

from core.app_settings_store import load_app_settings, update_app_settings
from core.auth_client import _protect_token, _unprotect_token
from core.purchase_tracking import inventory_item_match_key, recipe_substrate_inventory_key

API_BASE = "https://openapi.ecosteam.cn"
CREATE_PATH = "/Api/Furnace/CreateFurnace"
SELL_LIST_PATH = "/Api/Selling/GetSellGoodsList"
EDIT_PRICE_PATH = "/Api/Selling/GoodsPublishedBatchEdit"
LIST_PATH = "/Api/Furnace/MyFurnaceList"
PAY_PATH = "/Api/Furnace/FurnacePay"
STOCK_PATH = "/Api/Selling/QueryStock"
MARKET_LIST_PATH = "/Api/Market/SellGoodsList"
PUBLISH_STOCK_PATH = "/Api/Selling/PublishStock"
FURNACE_STATES = {100: "拼炉中", 200: "汰换中", 300: "公示中", 301: "客服介入", 350: "竞价中", 400: "已完成", 401: "拼炉失败", 402: "审核失败"}
_SETTINGS_KEY = "eco_open_api"


class EcoApiError(ValueError):
    def __init__(self, message: str, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


@dataclass(repr=False, frozen=True)
class EcoCredentials:
    partner_id: str
    private_key: str
    service_provider_id: str = ""
    open_id: str = ""

    def identity_params(self) -> dict:
        if self.service_provider_id or self.open_id:
            if not self.service_provider_id or not self.open_id:
                raise EcoApiError("服务商模式需要同时填写 ServiceProviderId 和 OpenID")
            return {"ServiceProviderId": self.service_provider_id, "OpenID": self.open_id}
        if not self.partner_id:
            raise EcoApiError("请先在 API 设置中填写 PartnerId")
        return {"PartnerId": self.partner_id}


def load_private_key(value: str):
    from Crypto.PublicKey import RSA
    try:
        encoded = value.strip().encode("ascii")
        if b"-----BEGIN" in encoded:
            encoded = re.sub(rb"-----[^-]+-----", b"", encoded)
        encoded = base64.b64decode(b"".join(encoded.split()), validate=True)
        key = RSA.import_key(encoded)
        if not key.has_private() or key.size_in_bits() < 2048:
            raise ValueError
        return key
    except (ValueError, TypeError, IndexError, UnicodeError) as exc:
        raise EcoApiError("请提供至少 2048 位的 RSA 私钥，支持 PEM 文件或 Base64 内容") from None


def generate_key_pair() -> tuple[str, str]:
    from Crypto.PublicKey import RSA
    key = RSA.generate(2048)
    return key.export_key(pkcs=8).decode("ascii"), base64.b64encode(key.public_key().export_key(format="DER")).decode("ascii")


def public_key_for(private_key: str) -> str:
    key = load_private_key(private_key)
    return base64.b64encode(key.public_key().export_key(format="DER")).decode("ascii")


def canonical_sign_text(params: dict) -> str:
    """与 ECO 官方 Python 示例一致：仅顶层按不区分大小写排序。"""
    def stringify(value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return str(value)
    return "&".join(f"{key}={stringify(params[key])}" for key in sorted(params, key=str.lower) if key.lower() != "sign")


def sign_params(params: dict, private_key: str) -> str:
    from Crypto.Hash import SHA256
    from Crypto.Signature import pkcs1_15
    signature = pkcs1_15.new(load_private_key(private_key)).sign(SHA256.new(canonical_sign_text(params).encode("utf-8")))
    return base64.b64encode(signature).decode("ascii")


def save_eco_credentials(credentials: EcoCredentials) -> None:
    credentials.identity_params()
    load_private_key(credentials.private_key)
    payload = {"partner_id": credentials.partner_id, "private_key_protected": _protect_token(credentials.private_key),
               "service_provider_id": credentials.service_provider_id, "open_id": credentials.open_id}
    if not update_app_settings(updates={_SETTINGS_KEY: payload}):
        raise OSError("ECO 配置保存失败")


def load_eco_credentials() -> EcoCredentials:
    raw = load_app_settings().get(_SETTINGS_KEY) or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        key = _unprotect_token(str(raw.get("private_key_protected") or ""))
    except (ValueError, OSError):
        key = ""
    return EcoCredentials(str(raw.get("partner_id") or ""), key, str(raw.get("service_provider_id") or ""), str(raw.get("open_id") or ""))


def money_value(value) -> float:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= 0:
            raise InvalidOperation
        return float(amount)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise EcoApiError("每件材料的售价必须是大于 0 的有效金额") from None


def match_recipe_inventory(recipe: dict, inventory: list[dict]) -> list[dict]:
    """一对一匹配名称和精确磨损；已指定资产必须属于所选库存，不能悄悄换件。"""
    used = set()
    result = []
    for substrate in recipe.get("substrates_display") or []:
        key = recipe_substrate_inventory_key(substrate)
        explicit = str(substrate.get("steam_assetid") or substrate.get("asset_id") or "")
        item = next((item for item in inventory if str(item.get("assetid") or "") not in used
                     and str(item.get("assetid") or "")
                     and key and inventory_item_match_key(item) == key
                     and (not explicit or str(item.get("assetid")) == explicit)), None)
        asset_id = str(item.get("assetid") or "") if item else ""
        if asset_id:
            used.add(asset_id)
        result.append({"name": str(substrate.get("name") or ""), "float_value": substrate.get("float_value"),
                       "AssetId": asset_id, "Price": substrate.get("price", 0), "Description": ""})
    return result


def build_create_payload(steam_id: str, title: str, assets: list[dict], *, fee_rate=10, publish_state=101, free_open_fee=True, self_open=True) -> dict:
    if not str(steam_id).isdigit() or len(str(steam_id)) != 17:
        raise EcoApiError("请选择已绑定有效 SteamID 的库存账号")
    if not title.strip():
        raise EcoApiError("请填写合炉标题")
    if len(assets) not in (5, 10):
        raise EcoApiError("合炉必须包含 5 件或 10 件真实库存材料")
    asset_ids = [str(row.get("AssetId") or "").strip() for row in assets]
    if any(not value or not value.isdigit() for value in asset_ids) or len(set(asset_ids)) != len(asset_ids):
        raise EcoApiError("材料未全部匹配到唯一的 Steam 资产，请刷新库存并重新匹配")
    fee_rate = float(fee_rate)
    if not math.isfinite(fee_rate) or not 0 <= fee_rate <= 10:
        raise EcoApiError("开炉费应在 0% 至 10% 之间")
    if publish_state not in (101, 201):
        raise EcoApiError("请选择公开或私密合炉")
    return {"ReceiveSteamID": str(steam_id), "AssetList": [
        {"Price": money_value(row.get("Price")), "Description": str(row.get("Description") or ""), "AssetId": asset_id}
        for row, asset_id in zip(assets, asset_ids)
    ], "Title": title.strip(), "FurnaceOpenRate": fee_rate, "PublishState": publish_state,
        "IsFreeOpenFee": bool(free_open_fee), "SelfOpen": bool(self_open)}


class EcoFurnaceClient:
    def __init__(self, credentials: EcoCredentials, *, session=None):
        self.credentials = credentials
        self.session = session or requests.Session()

    def _post(self, path: str, payload: dict):
        if path not in (CREATE_PATH, LIST_PATH, PAY_PATH, STOCK_PATH, PUBLISH_STOCK_PATH, SELL_LIST_PATH, EDIT_PRICE_PATH, MARKET_LIST_PATH):
            raise EcoApiError("不支持的 ECO 接口")
        params = {**payload, **self.credentials.identity_params(), "Timestamp": str(int(time.time()))}
        params["Sign"] = sign_params(params, self.credentials.private_key)
        # 创建和支付不自动重试；禁止重定向，签名参数只发送至官方域名。
        try:
            response = self.session.post(API_BASE + path, data=json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "language": "2"}, timeout=(10, 30), allow_redirects=False)
            if response.status_code != 200:
                raise EcoApiError(f"ECO 返回 HTTP {response.status_code}；如刚才提交了创建或支付，请先查询结果再重试", uncertain=True)
            result = response.json()
        except EcoApiError:
            raise
        except requests.RequestException:
            raise EcoApiError("连接 ECO 失败或超时。创建或支付可能已经提交，请先查询 ECO 合炉结果，避免重复操作。", uncertain=True) from None
        except (ValueError, TypeError):
            raise EcoApiError("ECO 返回内容无法解析，请先查询合炉结果后再操作", uncertain=True) from None
        if not isinstance(result, dict) or "ResultCode" not in result:
            raise EcoApiError("ECO 响应格式异常，请先查询结果", uncertain=True)
        if str(result["ResultCode"]) != "0":
            raise EcoApiError(f"ECO {result['ResultCode']}：{str(result.get('ResultMsg') or '请求未成功')[:500]}")
        return result.get("ResultData")

    def create(self, payload: dict):
        return self._post(CREATE_PATH, payload)

    def list_furnaces(self, state=100, page=1):
        if state not in FURNACE_STATES or not 1 <= int(page) <= 9999999:
            raise EcoApiError("合炉查询条件无效")
        data = self._post(LIST_PATH, {"HL_FurnaceState": state, "PageIndex": int(page), "PageSize": 20})
        if not isinstance(data, dict) or not isinstance(data.get("PageResult"), list):
            raise EcoApiError("ECO 合炉列表响应格式异常")
        return data

    def list_all_furnaces(self, state=100):
        rows, page = [], 1
        while True:
            data = self.list_furnaces(state, page)
            current = data["PageResult"]
            rows.extend(current)
            if not current or len(rows) >= int(data.get("TotalRecord") or len(rows)):
                return rows
            page += 1
            time.sleep(0.25)

    def list_furnaces_with_prices(self, state=100, page=1):
        from core.purchase_batches import list_purchase_batches
        from core.eco_publication import recipe_publication
        data = self.list_furnaces(state, page)
        bindings = {}
        for _path, batch in list_purchase_batches():
            for entry in batch.get("recipes") or []:
                publication = recipe_publication(batch, entry)
                if publication.get("furnace_id"):
                    bindings[publication["furnace_id"]] = publication
        listings = {}
        for row in data["PageResult"]:
            binding = bindings.get(str(row.get("FurnaceID") or ""))
            row["_listing_total"] = None
            if not binding:
                row["_listing_total_hint"] = "未关联完整配方材料，无法确认上架总价"
                continue
            steam_id = binding["steam_id"]
            if steam_id not in listings:
                try:
                    listings[steam_id] = self.private_listings(steam_id)
                except EcoApiError as exc:
                    listings[steam_id] = {}
                    row["_listing_total_hint"] = str(exc)
            assets = binding["asset_ids"]
            try:
                prices = [Decimal(str(listings[steam_id][asset]["Price"])) for asset in assets]
                if not prices or any(not price.is_finite() or price <= 0 for price in prices):
                    raise ValueError("售价无效")
                row["_listing_total"] = float(sum(prices))
                row["_listing_total_hint"] = f"已核对 ECO 私密在售的 {len(prices)} 件配方材料实际售价"
            except (KeyError, ValueError, TypeError, InvalidOperation):
                row.setdefault("_listing_total_hint", "配方材料未全部匹配到私密在售记录，无法确认总价")
        return data

    def stock_items(self, steam_id: str, asset_ids: list[str]) -> dict[str, dict]:
        """Read exact account assets, including StockId required by private listing."""
        if not steam_id.isdigit() or len(steam_id) != 17:
            raise EcoApiError("材料所属 SteamID 无效")
        wanted = {str(value) for value in asset_ids if str(value)}
        if not wanted:
            raise EcoApiError("请先匹配真实库存材料")
        found = {}
        for page in range(1, 101):
            data = self._post(STOCK_PATH, {"SteamId": steam_id, "GameId": "730", "StockStatus": -1, "PageIndex": page, "PageSize": 100})
            if not isinstance(data, dict) or not isinstance(data.get("PageResult", []), (list, type(None))):
                raise EcoApiError("ECO 库存参考价响应格式异常")
            rows = data.get("PageResult") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                asset_id = str(row.get("AssetId") or "")
                if asset_id not in wanted or (row.get("SteamId") and str(row["SteamId"]) != steam_id):
                    continue
                found[asset_id] = row
            if wanted.issubset(found) or not rows:
                break
            try:
                total = int(data.get("TotalRecord") or 0)
            except (ValueError, TypeError):
                total = 0
            if total and page * 100 >= total:
                break
            time.sleep(0.25)  # 保持低于库存接口每秒 5 次的普通账户限制。
        return found

    def reference_prices(self, steam_id: str, asset_ids: list[str]) -> dict[str, float]:
        found = {}
        for asset_id, row in self.stock_items(steam_id, asset_ids).items():
            try:
                price = Decimal(str(row.get("Price")))
                if price.is_finite() and price >= 0:
                    found[asset_id] = float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except (InvalidOperation, ValueError, TypeError):
                pass
        return found

    def market_material_prices(self, steam_id: str, assets: list[dict]) -> dict[str, float]:
        """Quote the cheapest current listing in each asset's wear interval."""
        from core.data_utils import get_interval_value
        stock = self.stock_items(steam_id, [row["AssetId"] for row in assets])
        quotes, cache = {}, {}
        for asset in assets:
            asset_id = asset["AssetId"]
            item = stock.get(asset_id, {})
            name = str(item.get("HashName") or "")
            if not name:
                continue
            wear = float(asset["float_value"])
            low, high = get_interval_value(math.nextafter(wear, math.inf))
            key = (name, low, high)
            if key not in cache:
                if cache:
                    time.sleep(0.25)
                data = self._post(MARKET_LIST_PATH, {
                    "GameId": "730", "HashName": name, "StartPaintWear": low,
                    "EndPaintWear": high, "PageIndex": 1, "PageSize": 50, "GoodRange": 1,
                })
                prices = []
                for listing in (data or {}).get("PageResult") or []:
                    try:
                        value = Decimal(str(listing.get("SellingPrice")))
                        listing_wear = float(listing["PaintWear"])
                        if value.is_finite() and value > 0 and low <= listing_wear and (listing_wear < high or listing_wear == high == 1):
                            prices.append(value)
                    except (ValueError, TypeError, KeyError, InvalidOperation):
                        continue
                cache[key] = float(min(prices).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if prices else None
            if cache[key] is not None:
                quotes[asset_id] = cache[key]
        return quotes

    def private_listings(self, steam_id: str) -> dict[str, dict]:
        found, page = {}, 1
        while True:
            data = self._post(SELL_LIST_PATH, {"SteamId": steam_id, "GameId": "730", "IsSecret": True, "PageIndex": page, "PageSize": 100})
            if not isinstance(data, dict) or not isinstance(data.get("PageResult"), list):
                raise EcoApiError("ECO 私密在售列表响应异常")
            rows = data["PageResult"]
            for row in rows:
                for item in row.get("Items") or [row]:
                    if item.get("AssetId") and item.get("GoodsNum") and item.get("IsSecret", row.get("IsSecret")) is True:
                        found[str(item["AssetId"])] = item
            if not rows or page * 100 >= int(data.get("TotalRecord") or len(rows)):
                return found
            page += 1
            time.sleep(0.25)

    def publish_private_materials(self, steam_id: str, assets: list[dict]):
        stock = self.stock_items(steam_id, [row["AssetId"] for row in assets])
        listed = self.private_listings(steam_id) if any(str(row.get("Status")) == "3" or row.get("CanPublish") is not True for row in stock.values()) else {}
        items, edits, result = [], [], []
        for asset in assets:
            row = stock.get(asset["AssetId"], {})
            existing = listed.get(asset["AssetId"])
            if existing:
                edits.append({"GoodsNum": existing["GoodsNum"], "AssetId": asset["AssetId"], "SteamGameId": str(row.get("SteamGameId") or "730"), "SellingPrice": money_value(asset["Price"]), "Description": str(asset.get("Description") or "")})
                continue
            if not row.get("StockId") or row.get("CanPublish") is not True or str(row.get("Status")) == "3":
                raise EcoApiError(f"材料 {asset['AssetId']} 当前不可上架，且未找到对应的私密在售记录，请在 ECO 核对")
            items.append({"StockId": row["StockId"], "AssetId": asset["AssetId"],
                          "SteamGameId": str(row.get("SteamGameId") or "730"),
                          "Price": money_value(asset["Price"]), "Description": str(asset.get("Description") or "")})
        if items:
            result = self._post(PUBLISH_STOCK_PATH, {"IsSecret": True, "Assets": items})
            if not isinstance(result, list):
                raise EcoApiError("私密上架结果未能确认，请先在 ECO 查询材料状态", uncertain=True)
        if edits:
            if self._post(EDIT_PRICE_PATH, {"goodsBatchEditList": edits}) is not True:
                raise EcoApiError("ECO 未确认改价完成，请核对私密在售材料的实际价格", uncertain=True)
            result.extend({"AssetId": row["AssetId"], "IsSuccess": True} for row in edits)
        try:
            actual = self.private_listings(steam_id)
        except EcoApiError as exc:
            raise EcoApiError(f"已提交上架或改价，但实际售价未能核对：{exc}", uncertain=True) from None
        wanted = {row["AssetId"]: money_value(row["Price"]) for row in assets}
        for row in result:
            asset_id = str(row.get("AssetId") or "")
            if row.get("IsSuccess") is True and (asset_id not in actual or money_value(actual[asset_id].get("Price")) != wanted[asset_id]):
                row.update(IsSuccess=None, ErrorMsg="平台实际售价尚未核对一致，请核对后再继续")
        return result

    def pay(self, furnace_id: str):
        if not furnace_id.strip():
            raise EcoApiError("请先选择待支付的合炉")
        result = self._post(PAY_PATH, {"FurnaceID": furnace_id.strip()})
        if result is not True:
            raise EcoApiError("ECO 未确认支付成功，请先查询该炉状态，勿直接重复支付", uncertain=True)
        return result


def purchase_recipe_for_furnace(batch: dict, entry: dict) -> dict:
    """从采购记录恢复实际材料，固定所属账号和入库资产。"""
    from core.purchase_batches import purchase_entry_effective_recipe
    try:
        recipe = purchase_entry_effective_recipe(entry)
        for row in entry.get("materials") or []:
            substrate = recipe["substrates_display"][int(row["substrate_index"])]
            substrate["price"] = (row.get("replacement") or {}).get("purchase_price", row.get("price", substrate.get("price")))
            substrate["steam_assetid"] = str(row.get("matched_assetid") or "")
            substrate["steam_profile_id"] = str(batch.get("profile_id") or "")
            substrate["steam_id"] = str(batch.get("steam_id") or "")
        return recipe
    except (KeyError, IndexError, TypeError, ValueError):
        raise EcoApiError("采购配方材料记录不完整，请重新核对库存") from None


def purchase_recipe_eco_readiness(batch: dict, entry: dict, inventory: list[dict], *, now=None) -> tuple[bool, str]:
    from core.purchase_batches import purchase_batch_recipe_live_readiness
    steam_id = str(batch.get("steam_id") or "")
    if len(steam_id) != 17 or not steam_id.isdigit() or not batch.get("profile_id"):
        return False, "配方未绑定有效的 Steam 账号"
    ready, reason = purchase_batch_recipe_live_readiness(batch, entry, inventory, now=now)
    if not ready:
        return False, reason
    try:
        recipe = purchase_recipe_for_furnace(batch, entry)
        assets = match_recipe_inventory(recipe, inventory)
    except EcoApiError as exc:
        return False, str(exc)
    if len(assets) not in (5, 10) or any(not row["AssetId"].isdigit() for row in assets):
        return False, "材料未精确匹配到所选账号的库存"
    by_id = {str(item.get("assetid") or ""): item for item in inventory}
    for row in assets:
        item = by_id[row["AssetId"]]
        # marketable 是缓存中的可出售标记，不代表当前可交易状态。
        # 旧库存即便 CD 已到期仍可能保留 marketable=0/context=16；上面的
        # live_readiness 已按真实截止时间检查 CD、市场挂售和缺失资产。
        # 显式不可交易且没有到期时间可解释的材料，仍不允许发布。
        if item.get("tradable") is False and item.get("cooldown_ends_at") in (None, ""):
            return False, "材料当前不可交易，请刷新 Steam 库存"
    return True, "材料齐全，当前库存和冷却检查通过"
