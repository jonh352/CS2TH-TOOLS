"""Offline collection artwork imported from the companion CS2TH catalogue."""

import json
from functools import cache

from config import ASSETS_DIR


def _normalize(name: str) -> str:
    return "".join(ch for ch in name if not ch.isspace() and ch not in '\"\'“”‘’').replace(
        "武器箱收藏品", "收藏品"
    )


@cache
def _index() -> dict:
    try:
        raw = json.loads((ASSETS_DIR / "collection_icons/index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {_normalize(key): value for key, value in raw.items()}


def collection_icon_path(name: str):
    entry = _index().get(_normalize(name), {})
    filename = entry.get("icon", "")
    if not filename:
        return None
    path = ASSETS_DIR / "collection_icons" / filename
    return path if path.is_file() else None
