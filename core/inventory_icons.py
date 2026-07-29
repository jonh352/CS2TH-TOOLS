"""库存/模拟页饰品图：仅使用 meta（SkinTemplate*.jsonl、pid2img.json、可选覆盖表）+ 本地 weapon_images。"""

from __future__ import annotations

import json
import logging
from typing import Optional

from config import META_DIR, WEAPON_IMAGES_DIR

from .alchemy_quality import canonical_steam_market_hash_name
from .inventory_hide_names import strip_wear_suffix_from_steam_item_name
from .skin_template_meta_load import (
    get_pid2img_dict,
    iter_skin_mem_json_objects,
    iter_skin_normal_json_objects,
    iter_skin_st_json_objects,
    paint_index_key_for_pid2img,
)

logger = logging.getLogger(__name__)

_SKIN_META_LOADED = False
_STEAM_NAME_TO_PAINT_NORMAL: dict[str, str] = {}
_STEAM_NAME_TO_PAINT_ST: dict[str, str] = {}
_STEAM_NAME_TO_PAINT_MEM: dict[str, str] = {}
_PID2IMG_MAP: dict[str, str] = {}
_STEAM_BASE_TO_IMG: dict[str, str] = {}
_STEAM_BASE_IMG_LOADED = False


def _add_steam_names_to_paint_map(row: dict, dest: dict[str, str]) -> None:
    pi = row.get("paint_index")
    if pi is None:
        return
    pid = str(pi).strip()
    if not pid:
        return
    steam = row.get("steam")
    if not isinstance(steam, dict):
        return
    for v in steam.values():
        if isinstance(v, str):
            s = v.strip()
            if s:
                dest[s] = pid
                alt = s.replace("\u2122", "").strip()
                if alt and alt != s:
                    dest.setdefault(alt, pid)
                canon = canonical_steam_market_hash_name(s)
                if canon and canon != s:
                    dest.setdefault(canon, pid)


def _ensure_skin_template_indexes() -> None:
    """加载 SkinTemplate*.jsonl 与 pid2img.json（惰性一次）。"""
    global _SKIN_META_LOADED, _STEAM_NAME_TO_PAINT_NORMAL, _STEAM_NAME_TO_PAINT_ST, _STEAM_NAME_TO_PAINT_MEM, _PID2IMG_MAP
    if _SKIN_META_LOADED:
        return
    normal: dict[str, str] = {}
    stmap: dict[str, str] = {}
    memmap: dict[str, str] = {}
    pid2: dict[str, str] = {}
    try:
        for obj in iter_skin_normal_json_objects():
            _add_steam_names_to_paint_map(obj, normal)
        for obj in iter_skin_st_json_objects():
            _add_steam_names_to_paint_map(obj, stmap)
        for obj in iter_skin_mem_json_objects():
            _add_steam_names_to_paint_map(obj, memmap)
        pid2 = get_pid2img_dict()
    except Exception as e:
        logger.warning("加载库存皮肤 meta 索引失败: %s", e)
    _STEAM_NAME_TO_PAINT_NORMAL = normal
    _STEAM_NAME_TO_PAINT_ST = stmap
    _STEAM_NAME_TO_PAINT_MEM = memmap
    _PID2IMG_MAP = pid2
    _SKIN_META_LOADED = True


def _ensure_steam_base_image_overrides() -> None:
    """``meta/steam_base_to_img.json``：英名（无磨损）→ weapon_images 文件名，补全尚未写入 SkinTemplate 的图。"""
    global _STEAM_BASE_IMG_LOADED, _STEAM_BASE_TO_IMG
    if _STEAM_BASE_IMG_LOADED:
        return
    out: dict[str, str] = {}
    path = META_DIR / "steam_base_to_img.json"
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    key = canonical_steam_market_hash_name(str(k))
                    fn = str(v or "").strip()
                    if key and fn:
                        out[key] = fn
    except Exception as e:
        logger.warning("加载 steam_base_to_img.json 失败: %s", e)
    _STEAM_BASE_TO_IMG = out
    _STEAM_BASE_IMG_LOADED = True


def _inventory_item_is_stattrak(item: dict) -> bool:
    """与库存页一致：名称相关字段是否含 StatTrak。"""
    for key in ("name", "market_name", "market_hash_name"):
        raw = item.get(key) or ""
        if raw and "StatTrak" in raw:
            return True
    return False


def _inventory_item_is_souvenir(item: dict) -> bool:
    """与库存页筛选一致：英名含 Souvenir 或中文含「纪念品」。"""
    for key in ("name", "market_name", "market_hash_name"):
        raw = item.get(key) or ""
        if raw and ("Souvenir" in raw or "纪念品" in raw):
            return True
    return False


def _steam_name_to_paint_index(name: str, name_map: dict[str, str]) -> Optional[str]:
    if not name or not name_map:
        return None
    pid = name_map.get(name)
    if pid:
        return pid
    return name_map.get(canonical_steam_market_hash_name(name))


def _inventory_steam_display_name_candidates(item: dict) -> list[str]:
    """按 Steam 常用字段顺序去重，供与 meta 中 ``steam`` 英名精确匹配。"""
    out: list[str] = []
    seen: set[str] = set()
    for key in ("market_hash_name", "market_name", "name"):
        raw = (item.get(key) or "").strip()
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _weapon_image_path_from_paint_index(paint_raw: str, *, stat_trak: bool) -> Optional[str]:
    _ensure_skin_template_indexes()
    if not _PID2IMG_MAP or not paint_raw:
        return None
    pid_key = paint_index_key_for_pid2img(paint_raw, stat_trak=stat_trak)
    fn = (_PID2IMG_MAP.get(pid_key) or "").strip()
    if not fn:
        return None
    p = (WEAPON_IMAGES_DIR / fn).resolve()
    return str(p) if p.is_file() else None


def _paint_index_for_inventory_item(item: dict) -> Optional[str]:
    """按物品类型在对应 SkinTemplate*.jsonl 的 steam 英名索引中解析 paint_index。"""
    _ensure_skin_template_indexes()
    if _inventory_item_is_souvenir(item):
        name_maps = (_STEAM_NAME_TO_PAINT_MEM,)
    elif _inventory_item_is_stattrak(item):
        name_maps = (_STEAM_NAME_TO_PAINT_ST,)
    else:
        name_maps = (_STEAM_NAME_TO_PAINT_NORMAL,)
    for name_map in name_maps:
        for n in _inventory_steam_display_name_candidates(item):
            paint_raw = _steam_name_to_paint_index(n, name_map)
            if paint_raw:
                return paint_raw
    return None


def _weapon_image_path_from_steam_base_override(item: dict) -> Optional[str]:
    _ensure_steam_base_image_overrides()
    if not _STEAM_BASE_TO_IMG:
        return None
    for n in _inventory_steam_display_name_candidates(item):
        base = strip_wear_suffix_from_steam_item_name(n)
        if not base:
            continue
        key = canonical_steam_market_hash_name(base)
        fn = (_STEAM_BASE_TO_IMG.get(key) or "").strip()
        if not fn:
            continue
        p = (WEAPON_IMAGES_DIR / fn).resolve()
        if p.is_file():
            return str(p)
    return None


def weapon_image_path_from_meta(item: dict) -> Optional[str]:
    """
    用 ``SkinTemplate*.jsonl`` 的 ``steam`` 英名匹配库存条目，取 ``paint_index``，
    再经 ``pid2img``（``*`` 开头 pid 会经 ``mem_pid2pid.json`` 映射）解析本地图。
    未命中模板时再查 ``steam_base_to_img.json``（补全本地已有、meta 未收录的皮肤图）。
    """
    _ensure_skin_template_indexes()
    paint_raw = _paint_index_for_inventory_item(item)
    if paint_raw and _PID2IMG_MAP:
        path = _weapon_image_path_from_paint_index(
            paint_raw, stat_trak=_inventory_item_is_stattrak(item)
        )
        if path:
            return path
    return _weapon_image_path_from_steam_base_override(item)


def weapon_image_path_from_skin_template(template: object) -> Optional[str]:
    """炼金模拟等：先按 paint_index→pid2img；未命中时与库存一样走 steam_base_to_img 回退。"""
    pi = getattr(template, "paint_index", None)
    if pi is not None:
        pid_raw = str(pi).strip()
        if pid_raw:
            st = bool(getattr(template, "stat_trak", False))
            path = _weapon_image_path_from_paint_index(pid_raw, stat_trak=st)
            if path:
                return path
    steam = getattr(template, "steam", None)
    if isinstance(steam, dict):
        for v in steam.values():
            if isinstance(v, str) and v.strip():
                path = _weapon_image_path_from_steam_base_override(
                    {"market_hash_name": v.strip()}
                )
                if path:
                    return path
    return None


def resolve_inventory_item_icon_path(item: dict) -> Optional[str]:
    """库存卡片用图：仅本地 weapon_images（经 meta / 覆盖表匹配）。"""
    return weapon_image_path_from_meta(item)
