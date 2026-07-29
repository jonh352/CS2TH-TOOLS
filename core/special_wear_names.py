"""特殊磨损页：从 SkinTemplate*.jsonl 一次性构建「武器 | 皮肤」全名列表（不含磨损外观），结果仅驻留内存。"""

from __future__ import annotations

import json

from .alchemy_quality import normalize_name
from .skin_template_meta_load import iter_meta_skin_lines_in_order

_mem_names: list[str] | None = None


def _scan_names_from_disk() -> list[str]:
    seen: set[str] = set()
    for line in iter_meta_skin_lines_in_order():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        w = str(data.get("weapon_name") or "").strip()
        s = str(data.get("skin_name") or "").strip()
        if not w:
            continue
        if s:
            seen.add(normalize_name(f"{w}|{s}"))
        else:
            seen.add(normalize_name(w))
    return sorted(seen)


def get_skin_full_names_without_appearance() -> list[str]:
    """返回去重、排序后的展示名列表；进程内首次调用时扫描 meta，之后复用同一份列表。"""
    global _mem_names
    if _mem_names is not None:
        return _mem_names
    _mem_names = _scan_names_from_disk()
    return _mem_names
