"""武器箱 meta（WeaponBox*.jsonl）：与 SkinTemplate 相同，优先 zlib 嵌入，否则读 ``meta/`` 明文。"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from core.alchemy_quality import get_pid_map, normalize_name
from core.data_utils import QUALITY_MAP
from core.skin_template_meta_load import weapon_box_jsonl_text

logger = logging.getLogger(__name__)

_QUALITY_JSON_KEYS: tuple[str, ...] = (
    "Consumer",
    "Industrial",
    "MilSpec",
    "Restricted",
    "Classified",
    "Covert",
    "Extraordinary",
)

_CN_BY_EN_QUALITY: dict[str, str] = {en: cn for cn, en in QUALITY_MAP.items()}


def _quality_cn(en_key: str) -> str:
    return _CN_BY_EN_QUALITY.get(en_key, en_key)


def _parse_jsonl_objects(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _goods_name_for_template(t) -> str:
    w = (t.weapon_name or "").strip()
    s = (t.skin_name or "").strip()
    if s:
        return normalize_name(f"{w}|{s}")
    return normalize_name(w)


@lru_cache(maxsize=1)
def _weapon_box_pick_rows_cached() -> tuple[tuple[str, str, bool, tuple[str, ...]], ...]:
    """``(武器箱名, 中文品质, 是否暗金, 该箱该品质下全部枪的标准化全名)``；先非 ST 再 ST。"""
    pid_map = get_pid_map()
    rows: list[tuple[str, str, bool, tuple[str, ...]]] = []
    for is_st in (False, True):
        text = weapon_box_jsonl_text(st=is_st)
        if not text.strip():
            continue
        for d in _parse_jsonl_objects(text):
            box_name = str(d.get("weapon_box_name") or "").strip()
            if not box_name:
                continue
            for qkey in _QUALITY_JSON_KEYS:
                pids = d.get(qkey)
                if not isinstance(pids, list) or not pids:
                    continue
                names: list[str] = []
                seen: set[str] = set()
                for pid in pids:
                    ps = str(pid).strip()
                    if not ps:
                        continue
                    t = pid_map.get(ps)
                    if t is None:
                        continue
                    gn = _goods_name_for_template(t)
                    if not gn or gn in seen:
                        continue
                    seen.add(gn)
                    names.append(gn)
                if not names:
                    continue
                q_cn = _quality_cn(qkey)
                rows.append((box_name, q_cn, is_st, tuple(names)))
    return tuple(rows)


def get_weapon_box_pick_rows() -> list[tuple[str, str, bool, tuple[str, ...]]]:
    return list(_weapon_box_pick_rows_cached())


def clear_weapon_box_catalog_cache() -> None:
    _weapon_box_pick_rows_cached.cache_clear()
