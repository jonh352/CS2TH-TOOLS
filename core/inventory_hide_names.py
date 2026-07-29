"""库存页：按 meta 列表排除指定枪名（比对前去掉 Steam 名称中的磨损后缀）。"""

from __future__ import annotations

import json

from config import INVENTORY_HIDE_NAMES_FILE
from core.data_utils import APPEARANCE

# Steam 英文市场名常见后缀，与 ``get_wear_level`` / ``APPEARANCE_MAP`` 一致
_WEAR_SUFFIX_EN = (
    "Battle-Scarred",
    "Well-Worn",
    "Field-Tested",
    "Minimal Wear",
    "Factory New",
)


def strip_wear_suffix_from_steam_item_name(name: str) -> str:
    """
    去掉名称末尾的磨损说明，便于与 meta 中「无磨损后缀」的枪名比对。
    支持中文全角/半角括号与英文半角括号形式。
    """
    s = (name or "").strip()
    if not s:
        return s
    for w in APPEARANCE:
        for left, right in (("（", "）"), ("(", ")")):
            suf = f"{left}{w}{right}"
            if s.endswith(suf):
                return s[: -len(suf)].rstrip()
    for w in _WEAR_SUFFIX_EN:
        suf = f" ({w})"
        if s.endswith(suf):
            return s[: -len(suf)].rstrip()
    return s


def _read_exclude_names_file() -> frozenset[str]:
    path = INVENTORY_HIDE_NAMES_FILE
    if not path.is_file():
        return frozenset()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    out: set[str] = set()
    for x in raw:
        if isinstance(x, str):
            t = x.strip()
            if t:
                out.add(t)
    return frozenset(out)


_hide_names_mtime_ns: int | None = None
_hide_names_set: frozenset[str] = frozenset()


def load_inventory_hide_name_set() -> frozenset[str]:
    """排除名集合；随 ``inventory_hide_names.json`` 的 mtime 自动重载。"""
    global _hide_names_mtime_ns, _hide_names_set
    path = INVENTORY_HIDE_NAMES_FILE
    try:
        mtime_ns = path.stat().st_mtime_ns if path.is_file() else -1
    except OSError:
        mtime_ns = -1
    if mtime_ns == _hide_names_mtime_ns:
        return _hide_names_set
    _hide_names_mtime_ns = mtime_ns
    _hide_names_set = _read_exclude_names_file()
    return _hide_names_set


def is_inventory_item_hidden_by_name_list(item: dict, exclude: frozenset[str] | None = None) -> bool:
    """若任一常用名字段去掉磨损后与排除列表某项相同，则视为隐藏。"""
    if exclude is None:
        exclude = load_inventory_hide_name_set()
    if not exclude:
        return False
    for key in ("market_name", "name", "market_hash_name"):
        raw = (item.get(key) or "").strip()
        if not raw:
            continue
        base = strip_wear_suffix_from_steam_item_name(raw)
        if base in exclude or raw in exclude:
            return True
    return False


def clear_inventory_hide_names_cache() -> None:
    """测试或外部写文件后如需立即生效可调用（一般靠 mtime 已足够）。"""
    global _hide_names_mtime_ns, _hide_names_set
    _hide_names_mtime_ns = None
    _hide_names_set = frozenset()
