"""Saved-recipe purchase batches reconciled against per-account Steam inventory."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.alchemy_quality import (
    canonical_goods_name_for_lookup,
    get_template_from_goods_name,
    resolve_inventory_skin_template,
)
from core.data_utils import wear_as_float32
from core.inventory_steam_accounts import profile_inventory_data_path
from core.saved_recipes import list_saved_recipes, update_recipe_recipe_dict


PURCHASE_TRACKING_KEY = "purchase_tracking"
PURCHASE_TRACKING_SCHEMA = 1

STATUS_PENDING = "pending"
STATUS_ORDERED = "ordered"
STATUS_RECEIVED = "received"
STATUS_CANCELLED = "cancelled"
VALID_STATUSES = {
    STATUS_PENDING,
    STATUS_ORDERED,
    STATUS_RECEIVED,
    STATUS_CANCELLED,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wear_bits(value: object) -> str:
    try:
        wear = wear_as_float32(float(value))
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(wear) or wear < 0.0 or wear > 1.0:
        return ""
    return struct.pack("<f", wear).hex()


def _template_key_from_recipe_substrate(substrate: dict[str, Any]) -> str:
    template = get_template_from_goods_name(str(substrate.get("name") or ""))
    if template is not None:
        canonical_name = canonical_goods_name_for_lookup(
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        return (
            f"name:{canonical_name}|pid:{template.paint_index}:"
            f"st:{int(bool(template.stat_trak))}"
        )
    name = canonical_goods_name_for_lookup(str(substrate.get("name") or ""))
    return f"name:{name}" if name else ""


def _template_key_from_inventory_item(item: dict[str, Any]) -> str:
    template = resolve_inventory_skin_template(item)
    if template is not None:
        canonical_name = canonical_goods_name_for_lookup(
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        return (
            f"name:{canonical_name}|pid:{template.paint_index}:"
            f"st:{int(bool(template.stat_trak))}"
        )
    for key in ("market_name", "name", "market_hash_name"):
        name = canonical_goods_name_for_lookup(str(item.get(key) or ""))
        if name:
            return f"name:{name}"
    return ""


def recipe_substrate_inventory_key(substrate: dict[str, Any]) -> str:
    template_key = _template_key_from_recipe_substrate(substrate)
    wear_key = _wear_bits(substrate.get("float_value"))
    return f"{template_key}|wear:{wear_key}" if template_key and wear_key else ""


def recipe_substrate_template_key(substrate: dict[str, Any]) -> str:
    return _template_key_from_recipe_substrate(substrate)


def inventory_item_match_key(item: dict[str, Any]) -> str:
    template_key = _template_key_from_inventory_item(item)
    wear_key = _wear_bits(item.get("float", item.get("float_value")))
    return f"{template_key}|wear:{wear_key}" if template_key and wear_key else ""


def inventory_item_template_key(item: dict[str, Any]) -> str:
    return _template_key_from_inventory_item(item)


def load_profile_inventory_items(profile_id: str) -> list[dict[str, Any]]:
    path = profile_inventory_data_path(str(profile_id or "").strip())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _material_rows_for_recipe(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    substrates = recipe.get("substrates_display")
    if not isinstance(substrates, list):
        return []
    occurrence_by_key: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for index, substrate in enumerate(substrates):
        if not isinstance(substrate, dict):
            continue
        try:
            collected_price = float(substrate.get("price") or 0.0)
        except (TypeError, ValueError):
            collected_price = 0.0
        if not math.isfinite(collected_price) or collected_price < 0:
            collected_price = 0.0
        match_key = recipe_substrate_inventory_key(substrate)
        fallback = (
            canonical_goods_name_for_lookup(str(substrate.get("name") or ""))
            + "|"
            + _wear_bits(substrate.get("float_value"))
        )
        identity = match_key or fallback or f"row:{index}"
        occurrence = occurrence_by_key.get(identity, 0)
        occurrence_by_key[identity] = occurrence + 1
        row_id = hashlib.sha256(
            f"{identity}|occurrence:{occurrence}".encode("utf-8")
        ).hexdigest()[:24]
        rows.append(
            {
                "row_id": row_id,
                "substrate_index": index,
                "match_key": match_key,
                "name": str(substrate.get("name") or ""),
                "float_value": float(substrate.get("float_value") or 0.0),
                "price": collected_price,
                "status": STATUS_PENDING,
            }
        )
    return rows


def build_material_tracking_rows(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    return _material_rows_for_recipe(recipe)


def purchase_tracking(recipe: dict[str, Any]) -> dict[str, Any] | None:
    value = recipe.get(PURCHASE_TRACKING_KEY)
    return value if isinstance(value, dict) else None


def start_purchase_batch(
    recipe: dict[str, Any],
    *,
    profile_id: str,
    steam_id: str,
    account_name: str,
    inventory_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a fresh batch and snapshot only relevant pre-existing assets."""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("Steam 收货账号不能为空")
    materials = _material_rows_for_recipe(recipe)
    if not materials:
        raise ValueError("配方中没有可跟踪的底物")
    if any(not row.get("match_key") for row in materials):
        raise ValueError("配方中存在无法识别名称或磨损的底物")
    expected_keys = {row["match_key"] for row in materials if row.get("match_key")}
    baseline_asset_ids = sorted(
        {
            str(item.get("assetid") or "")
            for item in inventory_items
            if inventory_item_match_key(item) in expected_keys
            and str(item.get("assetid") or "")
        }
    )
    tracking: dict[str, Any] = {
        "schema": PURCHASE_TRACKING_SCHEMA,
        "profile_id": profile_id,
        "steam_id": str(steam_id or "").strip(),
        "account_name": str(account_name or "").strip() or "Steam",
        "started_at": _utc_now(),
        "baseline_asset_ids": baseline_asset_ids,
        "materials": materials,
    }
    recipe[PURCHASE_TRACKING_KEY] = tracking
    return tracking


def _tracking_materials(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    tracking = purchase_tracking(recipe)
    if tracking is None:
        return []
    materials = tracking.get("materials")
    if not isinstance(materials, list):
        return []
    return [row for row in materials if isinstance(row, dict)]


def tracking_material_for_substrate(
    recipe: dict[str, Any],
    substrate_index: int,
) -> dict[str, Any] | None:
    for row in _tracking_materials(recipe):
        try:
            index = int(row.get("substrate_index", -1))
        except (TypeError, ValueError):
            continue
        if index == int(substrate_index):
            return row
    return None


def set_material_purchase_status(
    recipe: dict[str, Any],
    substrate_index: int,
    status: str,
) -> bool:
    row = tracking_material_for_substrate(recipe, substrate_index)
    if row is None or status not in VALID_STATUSES:
        return False
    old_status = str(row.get("status") or STATUS_PENDING)
    if old_status == status:
        return False
    row["status"] = status
    if status == STATUS_ORDERED:
        row["ordered_at"] = row.get("ordered_at") or _utc_now()
        row.pop("matched_assetid", None)
        row.pop("received_at", None)
    elif status == STATUS_RECEIVED:
        row["received_at"] = row.get("received_at") or _utc_now()
    else:
        row.pop("matched_assetid", None)
        row.pop("received_at", None)
        if status == STATUS_PENDING:
            row.pop("ordered_at", None)
    return True


def mark_all_materials_ordered(recipe: dict[str, Any]) -> int:
    changed = 0
    for row in _tracking_materials(recipe):
        if str(row.get("status") or STATUS_PENDING) != STATUS_PENDING:
            continue
        row["status"] = STATUS_ORDERED
        row["ordered_at"] = _utc_now()
        changed += 1
    return changed


def purchase_tracking_summary(recipe: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in VALID_STATUSES}
    materials = _tracking_materials(recipe)
    for row in materials:
        status = str(row.get("status") or STATUS_PENDING)
        counts[status if status in counts else STATUS_PENDING] += 1
    counts["total"] = len(materials)
    return counts


def _ordered_sort_key(
    task: tuple[Path, dict[str, Any], dict[str, Any]],
) -> tuple[str, str, str]:
    _path, tracking, row = task
    return (
        str(row.get("ordered_at") or tracking.get("started_at") or ""),
        str(tracking.get("started_at") or ""),
        str(row.get("row_id") or ""),
    )


def reconcile_saved_recipes_for_profile(
    profile_id: str,
    inventory_items: list[dict[str, Any]],
    *,
    excluded_asset_ids: set[str] | None = None,
) -> dict[str, int]:
    """One-to-one match newly arrived assets to ordered materials across recipes."""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return {
            "matched": 0,
            "waiting": 0,
            "tracked_recipes": 0,
            "save_failures": 0,
        }

    entries: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    used_asset_ids: set[str] = {
        str(assetid) for assetid in (excluded_asset_ids or set()) if str(assetid)
    }
    for path, payload in list_saved_recipes():
        source_recipe = payload.get("recipe")
        if not isinstance(source_recipe, dict):
            continue
        recipe = copy.deepcopy(source_recipe)
        tracking = purchase_tracking(recipe)
        if tracking is None or str(tracking.get("profile_id") or "") != profile_id:
            continue
        entries.append((path, recipe, tracking))
        for row in _tracking_materials(recipe):
            assetid = str(row.get("matched_assetid") or "")
            if str(row.get("status") or "") == STATUS_RECEIVED and assetid:
                used_asset_ids.add(assetid)

    available_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in inventory_items:
        assetid = str(item.get("assetid") or "")
        key = inventory_item_match_key(item)
        if not assetid or not key or assetid in used_asset_ids:
            continue
        available_by_key.setdefault(key, []).append(item)
    for items in available_by_key.values():
        items.sort(key=lambda item: str(item.get("assetid") or ""))

    tasks: list[tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for path, recipe, tracking in entries:
        for row in _tracking_materials(recipe):
            if str(row.get("status") or STATUS_PENDING) == STATUS_ORDERED:
                tasks.append((path, recipe, tracking, row))
    tasks.sort(key=lambda task: _ordered_sort_key((task[0], task[2], task[3])))

    changed_paths: set[Path] = set()
    matched_by_path: dict[Path, int] = {}
    matched = 0
    for path, _recipe, tracking, row in tasks:
        key = str(row.get("match_key") or "")
        candidates = available_by_key.get(key) or []
        baseline = {str(value) for value in tracking.get("baseline_asset_ids", [])}
        chosen_index = next(
            (
                index
                for index, item in enumerate(candidates)
                if str(item.get("assetid") or "") not in baseline
                and str(item.get("assetid") or "") not in used_asset_ids
            ),
            None,
        )
        if chosen_index is None:
            continue
        item = candidates.pop(chosen_index)
        assetid = str(item.get("assetid") or "")
        row["status"] = STATUS_RECEIVED
        row["matched_assetid"] = assetid
        row["received_at"] = _utc_now()
        used_asset_ids.add(assetid)
        changed_paths.add(path)
        matched_by_path[path] = matched_by_path.get(path, 0) + 1
        matched += 1

    save_failures = 0
    failed_match_total = 0
    for path, recipe, _tracking in entries:
        if path in changed_paths:
            try:
                update_recipe_recipe_dict(path, recipe)
            except (OSError, ValueError):
                failed_matches = matched_by_path.get(path, 0)
                matched -= failed_matches
                failed_match_total += failed_matches
                save_failures += 1

    waiting = sum(
        1
        for _path, recipe, _tracking in entries
        for row in _tracking_materials(recipe)
        if str(row.get("status") or STATUS_PENDING) == STATUS_ORDERED
    )
    # Candidate copies for failed files contain received rows, while their
    # on-disk versions still contain the previous ordered rows.
    waiting += failed_match_total
    return {
        "matched": matched,
        "waiting": waiting,
        "tracked_recipes": len(entries),
        "save_failures": save_failures,
    }


def received_asset_ids_for_saved_recipes(profile_id: str) -> set[str]:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return set()
    asset_ids: set[str] = set()
    for _path, payload in list_saved_recipes():
        recipe = payload.get("recipe")
        if not isinstance(recipe, dict):
            continue
        tracking = purchase_tracking(recipe)
        if tracking is None or str(tracking.get("profile_id") or "") != profile_id:
            continue
        for row in _tracking_materials(recipe):
            if str(row.get("status") or "") != STATUS_RECEIVED:
                continue
            assetid = str(row.get("matched_assetid") or "")
            if assetid:
                asset_ids.add(assetid)
    return asset_ids
