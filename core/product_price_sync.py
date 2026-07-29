"""Build and refresh the alchemy price cache from a CS2TH spot snapshot."""

from __future__ import annotations

import bisect
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .alchemy_quality import _iter_meta_skin_templates
from .data_utils import QUALITY_MAP


WEAR_UPPER = {
    "崭新出厂": 0.07,
    "略有磨损": 0.15,
    "久经沙场": 0.38,
    "破损不堪": 0.45,
    "战痕累累": 1.0,
}


def _normalized(value: float, minimum: float, maximum: float) -> float:
    span = maximum - minimum
    if span <= 0:
        return 0.0
    return round(max(0.0, min(1.0, (value - minimum) / span)), 10)


def _read_snapshot(
    path: Path,
) -> tuple[
    dict[str, list[tuple[float, float]]],
    dict[str, float],
    dict[str, str],
]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        bucket_rows = connection.execute(
            """
            SELECT market_hash_name, bucket_hi, price_cny
            FROM bucket_min_prices
            WHERE price_cny > 0
            ORDER BY market_hash_name, bucket_hi
            """
        ).fetchall()
        base_rows = connection.execute(
            "SELECT market_hash_name, price_cny FROM prices WHERE price_cny > 0"
        ).fetchall()
        metadata = dict(
            connection.execute("SELECT key, value FROM snapshot_meta").fetchall()
        )
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for name, upper, price in bucket_rows:
        buckets[str(name)].append((float(upper), float(price)))
    return buckets, {str(name): float(price) for name, price in base_rows}, metadata


def snapshot_metadata(path: Path) -> dict[str, str]:
    """Read only the tiny version metadata table from a snapshot."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return {
            str(key): str(value)
            for key, value in connection.execute(
                "SELECT key, value FROM snapshot_meta"
            ).fetchall()
        }


def _curve_for_template(
    template: Any,
    buckets: dict[str, list[tuple[float, float]]],
    base_prices: dict[str, float],
) -> dict[float, float]:
    curve: dict[float, float] = {}
    for wear, market_name in template.steam.items():
        if not market_name:
            continue
        rows = buckets.get(str(market_name), ())
        if rows:
            for upper, price in rows:
                curve[
                    _normalized(upper, template.min_float, template.max_float)
                ] = price
            continue
        price = base_prices.get(str(market_name))
        if price is None:
            continue
        upper = min(template.max_float, WEAR_UPPER.get(str(wear), template.max_float))
        if upper >= template.min_float:
            curve[_normalized(upper, template.min_float, template.max_float)] = price
    return curve


def _price_at(curve: dict[float, float], nfv: float) -> float:
    if not curve:
        return 0.0
    points = sorted(curve)
    index = bisect.bisect_left(points, nfv)
    point = points[-1] if index >= len(points) else points[index]
    return round(float(curve[point]), 2)


def build_product_price_payload(snapshot_path: Path) -> dict:
    """Convert the CS2TH spot SQLite schema to the desktop alchemy schema."""
    buckets, base_prices, metadata = _read_snapshot(snapshot_path)
    grouped = {
        "ordinary": defaultdict(list),
        "stat_trak": defaultdict(list),
    }
    curves: dict[tuple[str, str], dict[float, float]] = {}
    for template in _iter_meta_skin_templates():
        mode = "stat_trak" if template.stat_trak else "ordinary"
        curves[(mode, template.paint_index)] = _curve_for_template(
            template, buckets, base_prices
        )
        for box_id in template.weapon_box_id:
            grouped[mode][(str(box_id), template.quality)].append(template)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "fetch_time": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "source": "cs2th-spot-snapshot",
        "snapshot_synced_at": metadata.get("synced_at", ""),
        "snapshot_item_count": int(metadata.get("item_count", 0)),
        "ordinary": {},
        "stat_trak": {},
    }
    for mode in ("ordinary", "stat_trak"):
        mode_output = payload[mode]
        for (box_id, quality), box_templates in grouped[mode].items():
            all_nfvs = sorted(
                {
                    nfv
                    for template in box_templates
                    for nfv in curves[(mode, template.paint_index)]
                }
            )
            if not all_nfvs:
                continue
            quality_output = {}
            previous_leaf = None
            for nfv in all_nfvs:
                leaf = {
                    str(template.paint_index): _price_at(
                        curves[(mode, template.paint_index)], nfv
                    )
                    for template in box_templates
                }
                if leaf == previous_leaf:
                    continue
                quality_output[str(nfv)] = leaf
                previous_leaf = leaf
            if quality_output:
                mode_output.setdefault(box_id, {})[
                    QUALITY_MAP.get(quality, quality)
                ] = quality_output
    return payload


def merge_missing_groups(payload: dict, fallback_path: Path | None) -> None:
    """Retain older groups when the latest snapshot lacks their source listings."""
    if fallback_path is None or not fallback_path.is_file():
        return
    try:
        fallback = json.loads(fallback_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    for mode in ("ordinary", "stat_trak"):
        destination = payload[mode]
        for box_id, qualities in fallback.get(mode, {}).items():
            if box_id not in destination:
                destination[box_id] = qualities
                continue
            for quality, curve in qualities.items():
                destination[box_id].setdefault(quality, curve)
    payload["fallback_source"] = fallback_path.name


def write_product_price_payload(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(output_path)


def sync_product_price_cache(
    snapshot_path: Path,
    output_path: Path,
    current_payload: dict | None = None,
) -> tuple[dict, bool]:
    """Refresh only when the snapshot version differs; return payload and change."""
    metadata = snapshot_metadata(snapshot_path)
    current = current_payload if isinstance(current_payload, dict) else None
    if current is not None and str(current.get("snapshot_synced_at", "")) == str(
        metadata.get("synced_at", "")
    ):
        return current, False

    payload = build_product_price_payload(snapshot_path)
    merge_missing_groups(payload, output_path)
    write_product_price_payload(payload, output_path)
    return payload, True
