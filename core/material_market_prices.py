"""Read-only, same-wear-window material quotes across ECO and BUFF."""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor

from core.alchemy_quality import get_template_from_goods_name
from core.data_utils import get_interval_value
from core.market_candidates import fetch_exact_wear_candidates


def material_market_prices(client, steam_id: str, assets: list[dict], *, skipped: set[str] | None = None) -> dict:
    by_asset = {asset["AssetId"]: {} for asset in assets}
    errors = []
    skipped = skipped or set()
    def collect_eco():
        try:
            for asset_id, price in (client.market_material_prices(steam_id, assets) if "eco" not in skipped else {}).items():
                by_asset[asset_id]["ECO"] = price
        except ValueError as exc:
            errors.append(f"ECO：{exc}")

    def collect_platform(provider, label):
        if provider in skipped:
            return
        cache = {}
        try:
            for asset in assets:
                template = get_template_from_goods_name(asset["name"])
                if template is None:
                    continue
                low, high = get_interval_value(math.nextafter(float(asset["float_value"]), math.inf))
                key = (template.weapon_name, template.paint_index, template.stat_trak, low, high)
                if key not in cache:
                    if cache:
                        time.sleep(3)
                    rows = fetch_exact_wear_candidates(
                        provider, template=template, display_name=asset["name"],
                        min_wear=low, max_wear=high, max_pages=1,
                        request_interval=3,
                    )
                    prices = [float(row["price"]) for row in rows
                              if math.isfinite(float(row["price"])) and float(row["price"]) > 0
                              and low <= float(row["float_value"])
                              and (float(row["float_value"]) < high or float(row["float_value"]) == high == 1)]
                    cache[key] = min(prices) if prices else None
                if cache[key] is not None:
                    by_asset[asset["AssetId"]][label] = cache[key]
        except Exception as exc:
            errors.append(f"{label}：{exc}")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="material-quote") as executor:
        jobs = [executor.submit(collect_eco)]
        jobs += [executor.submit(collect_platform, provider, label)
                 for provider, label in (("buff", "BUFF"),)]
        for job in jobs:
            job.result()
    return {
        "prices": {asset_id: min(prices.values()) for asset_id, prices in by_asset.items() if prices},
        "platforms": by_asset,
        "errors": errors,
    }
