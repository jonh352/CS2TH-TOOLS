"""Independent Steam inventory-history events used by the statistics page."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from datetime import timezone
from threading import RLock

from core.steam_inventory_history import history_event_datetime, is_tradeup_history_event

_LOCK = RLock()


def _store_path():
    from core import purchase_batches as purchases
    return purchases.PURCHASE_BATCHES_DIR.parent / "steam_tradeup_events.json"


def _read_events() -> list[dict]:
    try:
        value = json.loads(_store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("Steam 汰换历史缓存格式无效，未覆盖原文件")
    return [row for row in value if isinstance(row, dict)]


def _item_identity(item: dict) -> tuple[str, ...]:
    if item.get("classid"):
        return tuple(str(item.get(key) or "") for key in ("appid", "classid", "instanceid", "assetid"))
    return tuple(str(item.get(key) or "") for key in (
        "appid", "classid", "instanceid", "assetid", "market_hash_name", "market_name", "name",
    ))


def save_steam_tradeup_events(profile_id: str, events: list[dict]) -> int:
    """Upsert real Steam events without changing purchase completion flags."""
    from core import purchase_batches as purchases
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("Steam 账号不能为空")
    profile = next((row for row in purchases.list_profile_entries() if str(row.get("id")) == profile_id), {})
    account_name = purchases.combo_display_name_for_profile(profile) if profile else profile_id
    steam_id = str(purchases.load_steam_account_config_dict(profile_id).get("steam_id") or "")
    occurrence: Counter[str] = Counter()
    incoming = {}
    for event in events:
        if not isinstance(event, dict) or not is_tradeup_history_event(event):
            continue
        when = history_event_datetime(event)
        if when is None:
            raise ValueError("Steam 汰换事件的实际时间未能识别，本次未更新统计")
        # Hash actual event content, never the refresh time or local completion time.
        identity = [when.astimezone(timezone.utc).isoformat(), sorted(_item_identity(row) for row in event["inputs"]),
                    sorted(_item_identity(row) for row in event["outputs"])]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
        occurrence[digest] += 1
        event_id = str(event.get("event_id") or f"{digest}:{occurrence[digest]}")
        key = f"{profile_id}:{event_id}"
        incoming[key] = {
            "id": key, "event_id": event_id, "source": "steam_inventory_history",
            "profile_id": profile_id, "steam_id": steam_id, "account_name": account_name,
            "completed_at": when.isoformat(),
            "inputs": copy.deepcopy(event["inputs"]), "outputs": copy.deepcopy(event["outputs"]),
        }
    with _LOCK:
        existing = {str(row.get("id") or ""): row for row in _read_events()}
        changed = sum(existing.get(key) != row for key, row in incoming.items())
        if changed:
            existing.update(incoming)
            path = _store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(list(existing.values()), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
    return changed


def _positive_price(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _wear_reference_price(item: dict, price_map: dict) -> float | None:
    """Use the lowest cached price in the known exterior, without inventing a float."""
    from core.alchemy_calc import _quality_nfv_leaves
    from core.alchemy_quality import canonical_steam_market_hash_name, resolve_inventory_skin_template
    from core.data_utils import APPEARANCE, LARGE_VALUE_LIST
    if not price_map:
        return None
    template = resolve_inventory_skin_template(item)
    if template is None:
        return None
    name = canonical_steam_market_hash_name(str(item.get("market_hash_name") or ""))
    wear = next((key for key, value in template.steam.items() if canonical_steam_market_hash_name(value) == name), None)
    if wear not in APPEARANCE or template.max_float <= template.min_float:
        return None
    index = APPEARANCE.index(wear)
    span = template.max_float - template.min_float
    low = (max(template.min_float, ([0.0] + LARGE_VALUE_LIST)[index]) - template.min_float) / span
    high = (min(template.max_float, LARGE_VALUE_LIST[index]) - template.min_float) / span
    prices = []
    for box_id in template.weapon_box_id:
        previous = 0.0
        for upper, leaf in sorted(_quality_nfv_leaves(price_map, int(box_id), template.quality, template.stat_trak).items()):
            if upper > low + 1e-9 and previous < high - 1e-9:
                value = _positive_price(leaf.get(str(template.paint_index), leaf.get("*")))
                if value is not None:
                    prices.append(value)
            previous = upper
    return min(prices) if prices else None


def _snapshot(item: dict, inventory: dict, price_map: dict) -> dict:
    from core import purchase_batches as purchases
    asset_id = str(item.get("assetid") or item.get("asset_id") or "")
    current = inventory.get(asset_id, {}) if asset_id else {}
    row = {
        "asset_id": asset_id,
        "name": str(item.get("market_name") or item.get("market_hash_name") or item.get("name") or "未知饰品"),
        "market_hash_name": str(item.get("market_hash_name") or ""),
        "classid": str(item.get("classid") or ""),
        "float_value": current.get("float", current.get("float_value", item.get("float_value"))),
        "steam_icon_url": str(item.get("icon_url") or ""),
        "source": "steam_inventory_history",
    }
    row["price"] = _positive_price(current.get("buff_price", current.get("price")))
    if row["price"] is None:
        row["price"] = _positive_price(purchases.lookup_inventory_item_price_value(
            {**item, **row, "float": row.get("float_value")}, price_map,
        ))
    if row["price"] is None and row["float_value"] is None:
        row["price"] = _wear_reference_price(row, price_map)
        if row["price"] is not None:
            row["price_source"] = "wear_market_reference"
    return row


def list_steam_tradeup_records(profile_id: str = "") -> list[dict]:
    """Steam events define membership; local records can only supply exact asset costs."""
    from core import purchase_batches as purchases
    with _LOCK:
        events = copy.deepcopy(_read_events())
    events = [row for row in events if not profile_id or str(row.get("profile_id")) == profile_id]
    if not events:
        return []
    inventory_cache = {}
    local_records = purchases.list_tradeup_records(profile_id)
    price_map = purchases.try_build_product_price_map_from_disk()
    result = []
    for event in events:
        account = str(event.get("profile_id") or "")
        if account not in inventory_cache:
            inventory_cache[account] = {
                str(item.get("assetid") or ""): item
                for item in purchases.load_profile_inventory_items(account) if isinstance(item, dict)
            }
        materials = [_snapshot(item, inventory_cache[account], price_map) for item in event["inputs"]]
        products = [_snapshot(item, inventory_cache[account], price_map) for item in event["outputs"]]
        input_ids = Counter(row["asset_id"] for row in materials)
        candidates = [row for row in local_records
                      if row.get("profile_id") == account and "" not in input_ids
                      and Counter(str(item.get("asset_id") or "") for item in row.get("materials") or []) == input_ids]
        context = candidates[0] if len(candidates) == 1 else {}
        # Optional purchase metadata never changes the historical inputs, output or time.
        local_materials = {str(item.get("asset_id") or ""): item for item in context.get("materials") or []}
        for material in materials:
            local = local_materials.get(material["asset_id"], {})
            if _positive_price(local.get("price")) is not None:
                material["price"] = _positive_price(local["price"])
                material.pop("price_source", None)
            if material["float_value"] is None and local.get("float_value") is not None:
                material["float_value"] = local["float_value"]
        cost = context.get("material_cost")
        cost_source = "purchase_record" if cost is not None else "market_reference"
        if cost is None:
            prices = [row.get("price") for row in materials]
            cost = sum(prices) if all(value is not None for value in prices) else None
        output_prices = [row.get("price") for row in products]
        output_value = sum(output_prices) if all(value is not None for value in output_prices) else None
        record = {**event, "materials": materials, "products": products,
                  "output_asset_ids": [row["asset_id"] for row in products if row["asset_id"]],
                  "material_cost": cost, "material_cost_source": cost_source if cost is not None else "unknown",
                  "output_value": output_value,
                  "profit": output_value - cost if output_value is not None and cost is not None else None}
        for key in ("batch_path", "batch_name", "recipe_entry_id", "recipe_index", "recipe_title", "target_paint_index"):
            if key in context:
                record[key] = context[key]
        result.append(record)
    result.sort(key=lambda row: history_event_datetime(row), reverse=True)
    return result
