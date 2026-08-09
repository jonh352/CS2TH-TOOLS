"""User-defined material-collection presets (named schemes + wear ranges)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import COLLECTION_PRESETS_DIR

SCHEMA_VERSION = 1


def _presets_dir() -> Path:
    path = COLLECTION_PRESETS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _preset_path(preset_id: str) -> Path:
    return _presets_dir() / f"{preset_id}.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        min_wear = float(raw.get("min_wear"))
        max_wear = float(raw.get("max_wear"))
    except (TypeError, ValueError):
        return None
    if min_wear > max_wear:
        min_wear, max_wear = max_wear, min_wear
    min_wear = max(0.0, min(1.0, min_wear))
    max_wear = max(0.0, min(1.0, max_wear))
    return {
        "name": name,
        "min_wear": min_wear,
        "max_wear": max_wear,
    }


def normalize_preset_items(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return out
    for raw in items:
        item = _normalize_item(raw)
        if item is None:
            continue
        key = item["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _read_preset_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    preset_id = str(data.get("id") or path.stem).strip()
    title = str(data.get("title") or "").strip() or "未命名方案"
    items = normalize_preset_items(data.get("items"))
    saved_at = str(data.get("saved_at") or "").strip()
    return {
        "schema": SCHEMA_VERSION,
        "id": preset_id,
        "title": title,
        "saved_at": saved_at,
        "items": items,
        "_path": str(path),
    }


def list_collection_presets() -> list[dict[str, Any]]:
    """Return presets newest-first (by ``saved_at``, then title)."""
    entries: list[dict[str, Any]] = []
    root = _presets_dir()
    for path in root.glob("*.json"):
        data = _read_preset_file(path)
        if data is not None:
            entries.append(data)
    entries.sort(
        key=lambda item: (
            str(item.get("saved_at") or ""),
            str(item.get("title") or ""),
        ),
        reverse=True,
    )
    return entries


def load_collection_preset(preset_id: str) -> dict[str, Any] | None:
    preset_id = str(preset_id or "").strip()
    if not preset_id:
        return None
    return _read_preset_file(_preset_path(preset_id))


def save_collection_preset(
    *,
    title: str,
    items: list[dict[str, Any]],
    preset_id: str | None = None,
) -> dict[str, Any]:
    """Create or overwrite a preset. Returns the saved envelope (no ``_path``)."""
    clean_title = str(title or "").strip() or "未命名方案"
    clean_items = normalize_preset_items(items)
    pid = str(preset_id or "").strip() or uuid.uuid4().hex
    payload = {
        "schema": SCHEMA_VERSION,
        "id": pid,
        "title": clean_title,
        "saved_at": _utc_now_iso(),
        "items": clean_items,
    }
    path = _preset_path(pid)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dict(payload)


def rename_collection_preset(preset_id: str, title: str) -> dict[str, Any] | None:
    data = load_collection_preset(preset_id)
    if data is None:
        return None
    return save_collection_preset(
        title=title,
        items=list(data.get("items") or []),
        preset_id=str(data.get("id") or preset_id),
    )


def delete_collection_preset(preset_id: str) -> bool:
    path = _preset_path(str(preset_id or "").strip())
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def format_preset_saved_at(saved_at: str) -> str:
    raw = str(saved_at or "").strip()
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        local = dt.astimezone()
    except ValueError:
        return raw
    return (
        f"{local.year}年{local.month}月{local.day}日 "
        f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
    )
