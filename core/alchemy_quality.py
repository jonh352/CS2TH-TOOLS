"""炼金页品质标签 - 从 SkinTemplate 获取皮肤品质"""

import json
import re
import unicodedata
from functools import cache

from .data_utils import SkinTemplate, APPEARANCE, APPEARANCE_MAP
from .skin_template_meta_load import iter_meta_skin_lines_in_order

# Unicode 空白（\s）+ 常见零宽/格式字符（复制或 meta 中可能混入，\s 不一定覆盖）
_WHITESPACE_AND_INVISIBLE_RE = re.compile(
    r"[\s\u200b\u200c\u200d\ufeff\u2060\u00ad]+",
    flags=re.UNICODE,
)


def strip_all_whitespace(name: str) -> str:
    """去掉名称中所有空白与常见不可见字符。"""
    if not name or not isinstance(name, str):
        return ""
    return _WHITESPACE_AND_INVISIBLE_RE.sub("", name)


def _iter_meta_skin_templates():
    """按文件名顺序、行顺序产出 SkinTemplate（与原先各 ``@cache`` 加载器一致）。"""
    for line in iter_meta_skin_lines_in_order():
        yield SkinTemplate.from_dict(json.loads(line))


def normalize_name(name: str) -> str:
    """meta / 搜索候选 / 模板键：去 ASCII 空格、半角括号转全角、别名统一（与 SkinTemplate 读取一致）。"""
    s = name
    if not s or not isinstance(s, str):
        return ""
    s = strip_all_whitespace(s)
    s = s.replace("|", " | ")
    return s


@cache
def _appearance_suffix_candidates() -> tuple[str, ...]:
    out: list[str] = []
    for appearance in (*APPEARANCE, *APPEARANCE_MAP.keys()):
        for left, right in (("（", "）"), ("(", ")")):
            suf = normalize_name(f"{left}{appearance}{right}")
            if suf and suf not in out:
                out.append(suf)
    return tuple(out)


def strip_appearance_suffix_from_goods_name(goods_name: str) -> str:
    """去掉名称末尾的磨损后缀，兼容中英文字样与全/半角括号。"""
    name = normalize_name(goods_name)
    if not name:
        return ""
    for suffix in _appearance_suffix_candidates():
        if name.endswith(suffix):
            return name[: -len(suffix)].rstrip()
    return name


@cache
def get_name_map():
    name_to_template = {}
    for template in _iter_meta_skin_templates():
        weapon = (template.weapon_name or "").strip()
        skin = (template.skin_name or "").strip()
        if skin:
            raw = f"{weapon}|{skin}"
        else:
            raw = weapon
        name = normalize_name(raw)
        if not name:
            continue
        name_to_template[name] = template
    return name_to_template


@cache
def get_pid_map():
    pid_to_template = {}
    for template in _iter_meta_skin_templates():
        pid_to_template[template.paint_index] = template
    return pid_to_template


def get_template_from_goods_name(goods_name: str) -> SkinTemplate | None:
    try:
        name_to_template = get_name_map()
        name = strip_appearance_suffix_from_goods_name(goods_name)
        return name_to_template.get(name)
    except Exception:
        return None


def get_quality_from_goods_name(goods_name: str) -> str | None:
    template = get_template_from_goods_name(goods_name)
    return template.quality if template else None


def canonical_goods_name_for_lookup(goods_name: str) -> str:
    """统一底物匹配键用名称：优先落到 meta 标准名，否则仅去掉磨损后缀。"""
    template = get_template_from_goods_name(goods_name)
    if template is not None:
        weapon = (template.weapon_name or "").strip()
        skin = (template.skin_name or "").strip()
        raw = f"{weapon}|{skin}" if skin else weapon
        return normalize_name(raw)
    return strip_appearance_suffix_from_goods_name(goods_name)


def canonical_steam_market_hash_name(name: str) -> str:
    """与 meta 中 ``steam``（外观→英名）表项对齐：去首尾空白、折叠空白、Unicode 规范化。"""
    s = unicodedata.normalize("NFKC", (name or "").strip())
    if not s:
        return ""
    return re.sub(r"\s+", " ", s)


@cache
def get_market_hash_name_to_template() -> dict[str, SkinTemplate]:
    """Steam 英名（``steam`` 字段值）→ SkinTemplate；来自 SkinTemplate*.jsonl。"""
    out: dict[str, SkinTemplate] = {}
    for tmpl in _iter_meta_skin_templates():
        for v in tmpl.steam.values():
            if isinstance(v, str):
                k = canonical_steam_market_hash_name(v)
                if k:
                    out[k] = tmpl
    return out


def resolve_inventory_skin_template(item: dict) -> SkinTemplate | None:
    """库存 dict：优先用 market_hash_name 命中 meta；否则尝试 market_name / name（中文商品名）。"""
    mhn = (item.get("market_hash_name") or "").strip()
    if mhn:
        t = get_market_hash_name_to_template().get(canonical_steam_market_hash_name(mhn))
        if t is not None:
            return t
    for key in ("market_name", "name"):
        v = (item.get(key) or "").strip()
        if v:
            t = get_template_from_goods_name(v)
            if t is not None:
                return t
    return None
