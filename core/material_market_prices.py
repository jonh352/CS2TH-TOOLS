"""Material reference prices from the assistant price bundle."""
from __future__ import annotations

from core.alchemy_calc import (
    build_price_map, load_product_price_raw, lookup_inventory_item_price_value,
)


def material_reference_prices(assets: list[dict]) -> dict:
    raw = load_product_price_raw()
    price_map = build_price_map(raw)
    prices = {}
    missing = []
    for asset in assets:
        price = lookup_inventory_item_price_value(
            {"market_name": asset["name"], "float": asset["float_value"]}, price_map,
        )
        if price is None:
            missing.append(asset["name"])
        else:
            prices[asset["AssetId"]] = price
    return {
        "prices": prices,
        "fetch_time": str(raw.get("fetch_time") or ""),
        "errors": ["价格包未找到材料参考价：" + "、".join(dict.fromkeys(missing))] if missing else [],
    }
