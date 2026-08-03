"""Managed JSON snapshots created by material collection."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from config import COLLECTED_JSON_DIR

REQUIRED_COLLECTION_KEYS = frozenset(
    {"float_value", "goods_id", "goods_name", "platform", "price"}
)


def _safe_file_stem(value: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    stem = stem.strip(" ._")
    return stem[:80] or datetime.now().strftime("采集数据_%Y%m%d_%H%M%S")


def normalized_collection_rows(items: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for source in items:
        if not isinstance(source, dict) or REQUIRED_COLLECTION_KEYS - source.keys():
            continue
        try:
            float_value = float(source["float_value"])
            price = float(source["price"])
        except (TypeError, ValueError):
            continue
        row = {
            "float_value": float_value,
            "goods_id": str(source["goods_id"]),
            "goods_name": str(source["goods_name"]),
            "platform": str(source["platform"]),
            "price": price,
        }
        for key in ("purchase_link", "listing_id"):
            value = source.get(key)
            if value not in (None, ""):
                row[key] = value
        rows.append(row)
    return rows


def save_collected_json(items: list[dict], title: str) -> Path:
    rows = normalized_collection_rows(items)
    if not rows:
        raise ValueError("没有可保存的有效采集数据")
    COLLECTED_JSON_DIR.mkdir(parents=True, exist_ok=True)
    stem = _safe_file_stem(title)
    path = COLLECTED_JSON_DIR / f"{stem}.json"
    suffix = 2
    while path.exists():
        path = COLLECTED_JSON_DIR / f"{stem}_{suffix}.json"
        suffix += 1
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def list_collected_json() -> list[tuple[Path, list[dict]]]:
    if not COLLECTED_JSON_DIR.is_dir():
        return []
    entries: list[tuple[Path, list[dict]]] = []
    for path in COLLECTED_JSON_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_rows = payload if isinstance(payload, list) else [payload]
        rows = normalized_collection_rows(
            [row for row in raw_rows if isinstance(row, dict)]
        )
        if rows:
            entries.append((path, rows))
    entries.sort(key=lambda entry: entry[0].stat().st_mtime, reverse=True)
    return entries


def delete_collected_json(path: Path) -> bool:
    base = COLLECTED_JSON_DIR.resolve()
    target = Path(path).resolve()
    if target.parent != base or target.suffix.lower() != ".json":
        raise ValueError("无效的采集 JSON 文件")
    if not target.exists():
        return False
    target.unlink()
    return True
