"""炼金计算结果配方：读写 config.RECIPES_DIR（CACHE_DIR/recipes）下独立 JSON 文件。"""

from __future__ import annotations

import json
import uuid
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import RECIPES_DIR

SCHEMA_VERSION = 1

# 配方 ``substrates_display`` 单项可选：配方管理「操作」列与炼金「导入数据」共用
SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY = "alchemy_meta_excluded"
SUBSTRATE_ALCHEMY_META_LOCKED_KEY = "alchemy_meta_locked"

# 与配方同目录的文件夹索引文件名；glob 配方时需跳过
FOLDERS_INDEX_FILENAME = "folders.json"

# 新建/重命名文件夹时与其它文件夹重名；UI 可据此与「名称无效」等区分处理
ERR_DUPLICATE_RECIPE_FOLDER_NAME = "已存在同名文件夹"

# list_saved_recipes / get_recipe_folder_stats 共用；任意配方 JSON 变更后须 invalidate
_recipe_entries_cache: list[tuple[Path, dict[str, Any]]] | None = None

_LEGACY_SUMMARY_TITLE_MARKERS: tuple[str, ...] = (
    "成本",
    "期望",
    "收益率",
    "保本率",
    "产物归一化磨损",
)


def format_recipe_summary_line(recipe: dict[str, Any]) -> str:
    """与计算结果卡片一致的绿色摘要：从「成本」起，不含 Top{k} 收益率配方 前缀。"""
    cost = recipe.get("cost", 0)
    expectation = recipe.get("expectation", 0)
    rate = recipe.get("rate", 0)
    rate_pct = float(rate) * 100 if isinstance(rate, (int, float)) else 0.0
    avg_nfv = recipe.get("avg_nfv", 0)
    be = recipe.get("break_even_rate", 0)
    if isinstance(be, (int, float)):
        be_str = f"{be:.2%}"
    else:
        be_str = str(be)
    c = float(cost) if isinstance(cost, (int, float)) else 0.0
    ex = float(expectation) if isinstance(expectation, (int, float)) else 0.0
    nfv = float(avg_nfv) if isinstance(avg_nfv, (int, float, np.float32)) else 0.0
    return (
        f"成本：{c:.2f} | 期望：{ex:.2f} | "
        f"收益率：{rate_pct:.2f}% | 保本率：{be_str} | 产物归一化磨损：{nfv:.9f}"
    )


def recipe_boxes_display_title(recipe: dict[str, Any]) -> str | None:
    """从 ``substrates_display`` 归纳武器箱名：单箱「{箱名}合成」，多箱「箱A x 箱B」。无有效箱名时 None。"""
    subs = recipe.get("substrates_display")
    if not isinstance(subs, list):
        return None
    boxes: list[str] = []
    seen: set[str] = set()
    for s in subs:
        if not isinstance(s, dict):
            continue
        wb_raw = s.get("weapon_box")
        wb = str(wb_raw).strip() if wb_raw is not None else ""
        if not wb:
            continue
        if wb not in seen:
            seen.add(wb)
            boxes.append(wb)
    if not boxes:
        return None
    if len(boxes) == 1:
        return f"{boxes[0]}合成"
    return " x ".join(boxes)


def default_save_recipe_dialog_title(recipe: dict[str, Any]) -> str:
    """保存时默认配方名：优先使用箱名归纳；无法归纳时回退为「未命名配方」。"""
    t = recipe_boxes_display_title(recipe)
    if t:
        return t
    return "未命名配方"


def _is_legacy_summary_title(title: object) -> bool:
    s = str(title or "").strip()
    if not s:
        return False
    return all(marker in s for marker in _LEGACY_SUMMARY_TITLE_MARKERS)


def _migrate_legacy_recipe_title(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """读取旧配方时，将摘要型 title 迁移为默认箱名标题，并尽量回写到磁盘。"""
    recipe = data.get("recipe")
    if not isinstance(recipe, dict):
        return data
    if not _is_legacy_summary_title(data.get("title")):
        return data
    new_title = default_save_recipe_dialog_title(recipe)
    if str(data.get("title") or "").strip() == new_title:
        return data
    new_data = dict(data)
    new_data["title"] = new_title
    try:
        path.write_text(
            json.dumps(new_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # 回写失败时至少保证本次读取后的内存态已完成迁移。
        pass
    return new_data


def _validated_recipe_file_path(path: Path) -> Path:
    """校验并返回 recipes 目录内的合法配方 JSON 路径。"""
    base = RECIPES_DIR.resolve()
    rp = path.resolve()
    if rp.suffix.lower() != ".json" or rp.parent != base:
        raise ValueError("invalid recipe file path")
    if rp.name.lower() == FOLDERS_INDEX_FILENAME.lower():
        raise ValueError("invalid recipe file path")
    return rp


def _write_recipe_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_loaded_recipe_payload(
    path: Path,
    data: dict[str, Any],
    *,
    require_recipe_dict: bool,
    migrate_legacy_title: bool,
) -> dict[str, Any]:
    if require_recipe_dict and not isinstance(data.get("recipe"), dict):
        raise ValueError("invalid recipe file")
    if migrate_legacy_title:
        data = _migrate_legacy_recipe_title(path, data)
    return data


def _load_recipe_payload(
    path: Path,
    *,
    require_recipe_dict: bool = False,
    migrate_legacy_title: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """统一读取/解析/迁移配方 JSON；供本模块所有配方文件入口复用。"""
    rp = _validated_recipe_file_path(path)
    raw = rp.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("invalid recipe file") from e
    if not isinstance(data, dict):
        raise ValueError("invalid recipe file")
    data = _normalize_loaded_recipe_payload(
        rp,
        data,
        require_recipe_dict=require_recipe_dict,
        migrate_legacy_title=migrate_legacy_title,
    )
    return rp, data


def _folders_index_path() -> Path:
    return RECIPES_DIR / FOLDERS_INDEX_FILENAME


def _iter_recipe_json_files() -> list[Path]:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for p in RECIPES_DIR.glob("*.json"):
        if p.name.lower() == FOLDERS_INDEX_FILENAME.lower():
            continue
        out.append(p)
    return out


def _valid_folder_ids() -> set[str]:
    ids: set[str] = set()
    for f in load_recipe_folders():
        fid = str(f.get("id") or "").strip()
        if fid:
            ids.add(fid)
    return ids


def _normalize_save_folder_id(folder_id: str | None) -> str | None:
    """保存配方时：仅当 id 仍存在于索引时写入；否则视为未分类。"""
    if not folder_id or not str(folder_id).strip():
        return None
    fid = str(folder_id).strip()
    if fid not in _valid_folder_ids():
        return None
    return fid


def load_recipe_folders() -> list[dict[str, Any]]:
    """返回用户文件夹列表，每项含 id、name、order（整数）。"""
    p = _folders_index_path()
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    folders = data.get("folders")
    if not isinstance(folders, list):
        return []
    out: list[dict[str, Any]] = []
    for item in folders:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not fid or not name:
            continue
        try:
            order = int(item.get("order", 0))
        except (TypeError, ValueError):
            order = 0
        out.append({"id": fid, "name": name, "order": order})
    out.sort(key=lambda x: (x["order"], x["name"]))
    return out


def _save_recipe_folders_payload(folders: list[dict[str, Any]]) -> None:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    normalized: list[dict[str, Any]] = []
    for i, f in enumerate(folders):
        fid = str(f.get("id") or "").strip()
        name = str(f.get("name") or "").strip()
        if not fid or not name:
            continue
        normalized.append({"id": fid, "name": name, "order": i})
    payload = {"version": 1, "folders": normalized}
    _folders_index_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def create_recipe_folder(name: str) -> str:
    """新建文件夹，返回 id。"""
    n = (name or "").strip()
    if not n:
        raise ValueError("文件夹名称不能为空")
    folders = load_recipe_folders()
    for f in folders:
        if str(f.get("name") or "").strip() == n:
            raise ValueError(ERR_DUPLICATE_RECIPE_FOLDER_NAME)
    rid = uuid.uuid4().hex
    folders.append({"id": rid, "name": n, "order": len(folders)})
    _save_recipe_folders_payload(folders)
    return rid


def rename_recipe_folder(folder_id: str, name: str) -> None:
    fid = (folder_id or "").strip()
    n = (name or "").strip()
    if not fid or not n:
        raise ValueError("名称无效")
    folders = load_recipe_folders()
    seen_other = False
    for f in folders:
        if str(f.get("id")) == fid:
            continue
        if str(f.get("name") or "").strip() == n:
            seen_other = True
            break
    if seen_other:
        raise ValueError(ERR_DUPLICATE_RECIPE_FOLDER_NAME)
    found = False
    for f in folders:
        if str(f.get("id")) == fid:
            f["name"] = n
            found = True
            break
    if not found:
        raise ValueError("文件夹不存在")
    _save_recipe_folders_payload(folders)


def reorder_recipe_folders(ordered_ids: list[str]) -> None:
    """
    按 ordered_ids 的顺序重写各用户文件夹的 order。
    ordered_ids 须与当前 load_recipe_folders() 的 id 集合完全一致（无重复、无遗漏）。
    """
    folders = load_recipe_folders()
    if not folders:
        if ordered_ids:
            raise ValueError("文件夹列表与保存不一致")
        return
    id_set = {str(f.get("id")) for f in folders}
    oids = [str(x).strip() for x in ordered_ids if str(x).strip()]
    if len(oids) != len(id_set) or len(oids) != len(set(oids)):
        raise ValueError("文件夹列表与保存不一致")
    if set(oids) != id_set:
        raise ValueError("文件夹列表与保存不一致")
    id_to_folder: dict[str, dict[str, Any]] = {}
    for f in folders:
        fid = str(f.get("id") or "").strip()
        if fid:
            id_to_folder[fid] = dict(f)
    new_list: list[dict[str, Any]] = []
    for fid in oids:
        row = id_to_folder.get(fid)
        if row is None:
            raise ValueError("文件夹列表与保存不一致")
        new_list.append(row)
    _save_recipe_folders_payload(new_list)


def delete_recipe_folder(folder_id: str) -> int:
    """删除文件夹定义，并将其中的配方移回未分类。返回受影响的配方文件数。"""
    fid = (folder_id or "").strip()
    if not fid:
        raise ValueError("文件夹无效")
    folders = load_recipe_folders()
    new_list = [f for f in folders if str(f.get("id")) != fid]
    if len(new_list) == len(folders):
        raise ValueError("文件夹不存在")
    _save_recipe_folders_payload(new_list)
    n = 0
    for path in _iter_recipe_json_files():
        try:
            rp, data = _load_recipe_payload(path)
        except (OSError, ValueError):
            continue
        cur = str(data.get("folder_id") or "").strip()
        if cur == fid:
            data.pop("folder_id", None)
            try:
                _write_recipe_payload(rp, data)
                n += 1
            except OSError:
                continue
    if n:
        invalidate_saved_recipes_cache()
    return n


def delete_recipe_folder_and_recipes(folder_id: str) -> int:
    """删除文件夹定义，并永久删除该文件夹下全部配方 JSON。返回删除的配方文件数。"""
    fid = (folder_id or "").strip()
    if not fid:
        raise ValueError("文件夹无效")
    folders = load_recipe_folders()
    if not any(str(f.get("id")) == fid for f in folders):
        raise ValueError("文件夹不存在")
    paths: list[Path] = []
    for path in _iter_recipe_json_files():
        try:
            rp, data = _load_recipe_payload(path)
        except (OSError, ValueError):
            continue
        cur = str(data.get("folder_id") or "").strip()
        if cur == fid:
            paths.append(rp)
    n = delete_recipe_files(paths)
    new_list = [f for f in folders if str(f.get("id")) != fid]
    _save_recipe_folders_payload(new_list)
    return n


def _payload_folder_id(payload: dict[str, Any]) -> str:
    raw = payload.get("folder_id")
    if raw is None:
        return ""
    return str(raw).strip()


def _matches_folder_filter(payload: dict[str, Any], folder_filter: str | None) -> bool:
    """
    folder_filter:
      None — 全部
      "" — 仅未分类（无 folder_id）
      非空 str — 该文件夹 id
    """
    cur = _payload_folder_id(payload)
    if folder_filter is None:
        return True
    if folder_filter == "":
        return cur == ""
    return cur == folder_filter


def invalidate_saved_recipes_cache() -> None:
    """使配方列表内存缓存失效。修改 RECIPES_DIR 下任意配方 .json 后应调用（本模块内已统一处理）。"""
    global _recipe_entries_cache
    _recipe_entries_cache = None


def _scan_recipe_json_entries() -> list[tuple[Path, dict[str, Any]]]:
    """单次目录扫描：读取并解析全部合法配方文件（不排序）。"""
    out: list[tuple[Path, dict[str, Any]]] = []
    for p in _iter_recipe_json_files():
        try:
            rp, data = _load_recipe_payload(p, require_recipe_dict=True)
            out.append((rp, data))
        except (OSError, ValueError):
            continue
    return out


def _all_recipe_entries_cached() -> list[tuple[Path, dict[str, Any]]]:
    global _recipe_entries_cache
    if _recipe_entries_cache is None:
        _recipe_entries_cache = _scan_recipe_json_entries()
    return _recipe_entries_cache


def save_recipe_file(
    recipe: dict[str, Any],
    *,
    rank: int,
    mode: str,
    norm_min: float,
    norm_max: float,
    title: str | None = None,
    folder_id: str | None = None,
) -> Path:
    """
    将 UI 用 recipe 字典（无 solution 字段）写入 RECIPES_DIR/<uuid>.json。
    recipe 须含 cost、rate、substrates_display、products_display 等可 JSON 序列化字段。
    """
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    auto_title = default_save_recipe_dialog_title(recipe)
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": rid,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "title": (title or "").strip() or auto_title,
        "rank": rank,
        "mode": mode,
        "norm_min": float(norm_min),
        "norm_max": float(norm_max),
        "recipe": dict(recipe),
    }
    fid = _normalize_save_folder_id(folder_id)
    if fid:
        payload["folder_id"] = fid
    path = RECIPES_DIR / f"{rid}.json"
    _write_recipe_payload(path, payload)
    invalidate_saved_recipes_cache()
    return path


def list_saved_recipes(
    *,
    folder_filter: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """
    返回 (路径, 解析后的 JSON)，按 saved_at 新到旧排序。

    folder_filter: None=全部；\"\"=仅未分类；否则为具体文件夹 id。
    """
    out = [
        (p, data)
        for p, data in _all_recipe_entries_cached()
        if _matches_folder_filter(data, folder_filter)
    ]

    def sort_key(item: tuple[Path, dict]) -> str:
        return str(item[1].get("saved_at") or "")

    out.sort(key=sort_key, reverse=True)
    return out


def get_recipe_folder_stats() -> dict[str, Any]:
    """统计：total、uncategorized、by_folder{folder_id: n}。"""
    total = 0
    uncategorized = 0
    by_folder: dict[str, int] = {}
    for _p, data in _all_recipe_entries_cached():
        total += 1
        fid = _payload_folder_id(data)
        if not fid:
            uncategorized += 1
        else:
            by_folder[fid] = by_folder.get(fid, 0) + 1
    return {
        "total": total,
        "uncategorized": uncategorized,
        "by_folder": by_folder,
    }


def set_recipe_file_folder_id(path: Path, folder_id: str | None) -> None:
    """将配方文件移到某文件夹；folder_id 为 None 或空串表示未分类。"""
    rp, data = _load_recipe_payload(path)
    fid = _normalize_save_folder_id(folder_id) if folder_id else None
    if fid:
        data["folder_id"] = fid
    else:
        data.pop("folder_id", None)
    _write_recipe_payload(rp, data)
    invalidate_saved_recipes_cache()


def move_recipes_to_folder(paths: list[Path], folder_id: str | None) -> int:
    """批量移动；folder_id None 表示未分类。返回成功数。"""
    n = 0
    for p in paths:
        try:
            set_recipe_file_folder_id(p, folder_id)
            n += 1
        except ValueError:
            continue
    return n


def update_recipe_recipe_dict(path: Path, recipe: dict[str, Any]) -> None:
    """将磁盘上配方文件中的 recipe 字段整体替换为给定 dict（用于持久化 purchase_viewed 等）。"""
    rp, data = _load_recipe_payload(path)
    data["recipe"] = recipe
    _write_recipe_payload(rp, data)
    invalidate_saved_recipes_cache()


def delete_recipe_files(paths: list[Path]) -> int:
    """删除给定路径的配方文件，返回成功删除数量（仅允许删除 recipes 目录内 .json）。"""
    base = RECIPES_DIR.resolve()
    n = 0
    for p in paths:
        try:
            rp = p.resolve()
            if rp.suffix.lower() != ".json" or rp.parent != base:
                continue
            if rp.name.lower() == FOLDERS_INDEX_FILENAME.lower():
                continue
            rp.unlink()
            n += 1
        except OSError:
            continue
    if n:
        invalidate_saved_recipes_cache()
    return n
