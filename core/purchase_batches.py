"""Batch-level purchasing, Steam reconciliation, and replacement planning."""

from __future__ import annotations

import copy
import json
import math
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from config import PURCHASE_BATCHES_DIR
from core.alchemy_calc import (
    lookup_template_price_value,
    tradeup_average_normalized_float32,
    tradeup_product_wear_float32,
    try_build_product_price_map_from_disk,
)
from core.alchemy_quality import (
    get_name_map,
    get_pid_map,
    get_template_from_goods_name,
    resolve_inventory_skin_template,
)
from core.data_utils import (
    SkinInstance,
    SkinTemplate,
    tradeup_display_quality,
    wear_as_float32,
)
from core.inventory_steam_accounts import (
    combo_display_name_for_profile,
    list_profile_entries,
)
from core.purchase_tracking import (
    STATUS_CANCELLED,
    STATUS_ORDERED,
    STATUS_PENDING,
    STATUS_RECEIVED,
    VALID_STATUSES,
    build_material_tracking_rows,
    inventory_item_match_key,
    inventory_item_template_key,
    inventory_wear_matches_planned,
    load_profile_inventory_items,
    received_asset_ids_for_saved_recipes,
    reconcile_saved_recipes_for_profile,
    recipe_substrate_template_key,
)


PURCHASE_BATCH_SCHEMA = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_batch_path(path: Path) -> Path:
    base = PURCHASE_BATCHES_DIR.resolve()
    resolved = Path(path).resolve()
    if resolved.parent != base or resolved.suffix.lower() != ".json":
        raise ValueError("无效的采购批次文件")
    return resolved


def _write_batch(path: Path, payload: dict[str, Any]) -> None:
    target = _validated_batch_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(target)


def load_purchase_batch(path: Path) -> dict[str, Any]:
    target = _validated_batch_path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("采购批次文件已损坏") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("recipes"), list):
        raise ValueError("采购批次格式无效")
    return payload


def list_purchase_batches() -> list[tuple[Path, dict[str, Any]]]:
    if not PURCHASE_BATCHES_DIR.is_dir():
        return []
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in PURCHASE_BATCHES_DIR.glob("*.json"):
        try:
            entries.append((path, load_purchase_batch(path)))
        except (OSError, ValueError):
            continue
    entries.sort(
        key=lambda entry: str(entry[1].get("updated_at") or entry[1].get("created_at") or ""),
        reverse=True,
    )
    return entries


def create_purchase_batch(
    name: str,
    *,
    profile_id: str,
    steam_id: str,
    account_name: str,
    inventory_items: list[dict[str, Any]],
) -> Path:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("Steam 收货账号不能为空")
    batch_id = uuid.uuid4().hex
    now = _utc_now()
    payload: dict[str, Any] = {
        "schema": PURCHASE_BATCH_SCHEMA,
        "id": batch_id,
        "name": str(name or "").strip() or f"采购批次 {now[:10]}",
        "profile_id": profile_id,
        "steam_id": str(steam_id or "").strip(),
        "account_name": str(account_name or "").strip() or "Steam",
        "created_at": now,
        "updated_at": now,
        "baseline_asset_ids": sorted(
            {
                str(item.get("assetid") or "")
                for item in inventory_items
                if str(item.get("assetid") or "")
            }
        ),
        "recipes": [],
    }
    path = PURCHASE_BATCHES_DIR / f"{batch_id}.json"
    _write_batch(path, payload)
    return path


def add_recipe_to_purchase_batch(
    path: Path,
    recipe: dict[str, Any],
    *,
    title: str = "采集配方",
    source_ref: str = "",
) -> str:
    payload = load_purchase_batch(path)
    source_ref = str(source_ref or "").strip()
    if source_ref and any(
        isinstance(entry, dict)
        and str(entry.get("source_ref") or "").strip() == source_ref
        for entry in payload.get("recipes") or []
    ):
        raise ValueError("该计算结果已在此采购批次中")
    recipe_copy = copy.deepcopy(recipe)
    materials = build_material_tracking_rows(recipe_copy)
    if not materials or any(not row.get("match_key") for row in materials):
        raise ValueError("配方中存在无法识别名称或磨损的材料")
    entry_id = uuid.uuid4().hex
    added_at = _utc_now()
    payload["recipes"].append(
        {
            "id": entry_id,
            "title": str(title or "").strip() or "采集配方",
            "added_at": added_at,
            "source_ref": source_ref,
            "recipe": recipe_copy,
            "materials": materials,
        }
    )
    payload["updated_at"] = added_at
    _write_batch(path, payload)
    return entry_id


def delete_purchase_batch(path: Path) -> None:
    target = _validated_batch_path(path)
    if target.exists():
        target.unlink()


def purchase_batch_summary(batch: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in VALID_STATUSES}
    recipe_count = 0
    for entry in batch.get("recipes") or []:
        if not isinstance(entry, dict):
            continue
        recipe_count += 1
        for row in entry.get("materials") or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or STATUS_PENDING)
            counts[status if status in counts else STATUS_PENDING] += 1
    counts["total"] = sum(counts.values())
    counts["recipes"] = recipe_count
    counts["tradeup_completed_recipes"] = sum(
        1
        for entry in batch.get("recipes") or []
        if isinstance(entry, dict) and bool(entry.get("tradeup_completed"))
    )
    counts["missing_review"] = sum(
        1
        for entry in batch.get("recipes") or []
        if isinstance(entry, dict)
        and not bool(entry.get("tradeup_completed"))
        for row in entry.get("materials") or []
        if isinstance(row, dict)
        and str(row.get("status") or "") == STATUS_RECEIVED
        and bool(row.get("inventory_missing_since"))
    )
    return counts


def purchase_batch_is_fully_received(batch: dict[str, Any]) -> bool:
    summary = purchase_batch_summary(batch)
    return (
        summary["total"] > 0
        and summary[STATUS_RECEIVED] == summary["total"]
        and summary["missing_review"] == 0
    )


def purchase_batch_is_tradeup_completed(batch: dict[str, Any]) -> bool:
    """Return whether a non-empty batch has completed every recipe."""
    recipes = [entry for entry in batch.get("recipes") or [] if isinstance(entry, dict)]
    return bool(recipes) and all(bool(entry.get("tradeup_completed")) for entry in recipes)


def purchase_batch_section(batch: dict[str, Any]) -> str:
    """Stable UI section for a batch: purchasing, received, or completed."""
    if purchase_batch_is_tradeup_completed(batch):
        return "tradeup_completed"
    if purchase_batch_is_fully_received(batch):
        return "purchase_completed"
    return "purchasing"


def compute_alchemy_ready_at(verified_at: datetime | None = None) -> datetime:
    """Next whole hour after verification, plus 7 days (local timezone)."""
    when = verified_at or datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()
    else:
        when = when.astimezone()
    floored = when.replace(minute=0, second=0, microsecond=0)
    next_hour = floored + timedelta(hours=1)
    return next_hour + timedelta(days=7)


def _parse_alchemy_ready_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def format_alchemy_ready_at(value: object) -> str:
    ready = _parse_alchemy_ready_at(value)
    if ready is None:
        return ""
    local = ready.astimezone()
    return f"{local.year}-{local.month}-{local.day} {local.hour:02d}:{local.minute:02d}"


def purchase_batch_alchemy_status_text(
    batch: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Orange suffix after batch name: alchemy countdown text, or empty."""
    if not purchase_batch_is_fully_received(batch):
        return ""
    ready = _parse_alchemy_ready_at(batch.get("alchemy_ready_at"))
    if ready is None:
        return ""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    else:
        current = current.astimezone()
    if current >= ready:
        return "可炼金"
    return f"炼金时间:{format_alchemy_ready_at(ready)}"


def refresh_purchase_batch_alchemy_ready_at(
    path: Path,
    *,
    verified_at: datetime | None = None,
) -> str | None:
    """Record alchemy_ready_at once when a batch first becomes fully received."""
    batch = load_purchase_batch(path)
    summary = purchase_batch_summary(batch)
    if summary["missing_review"]:
        existing = str(batch.get("alchemy_ready_at") or "").strip()
        return existing or None
    if purchase_batch_is_fully_received(batch):
        existing = str(batch.get("alchemy_ready_at") or "").strip()
        if existing:
            return existing
        ready = compute_alchemy_ready_at(verified_at)
        batch["alchemy_ready_at"] = ready.isoformat()
        batch["updated_at"] = _utc_now()
        _write_batch(path, batch)
        return str(batch["alchemy_ready_at"])
    if "alchemy_ready_at" in batch:
        batch.pop("alchemy_ready_at", None)
        batch["updated_at"] = _utc_now()
        _write_batch(path, batch)
    return None


def _find_material(
    batch: dict[str, Any],
    recipe_entry_id: str,
    row_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for entry in batch.get("recipes") or []:
        if not isinstance(entry, dict) or str(entry.get("id") or "") != recipe_entry_id:
            continue
        for row in entry.get("materials") or []:
            if isinstance(row, dict) and str(row.get("row_id") or "") == row_id:
                return entry, row
    return None


def set_purchase_batch_recipe_tradeup_completed(
    path: Path,
    recipe_entry_id: str,
    completed: bool,
) -> bool:
    """Persist whether a recipe has been consumed in a trade-up.

    Completed recipes keep their received-asset history, but those assets no
    longer participate in inventory-departure checks.
    """
    batch = load_purchase_batch(path)
    entry = next(
        (
            candidate
            for candidate in batch.get("recipes") or []
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == str(recipe_entry_id or "")
        ),
        None,
    )
    if entry is None or bool(entry.get("tradeup_completed")) == bool(completed):
        return False
    now = _utc_now()
    if completed:
        entry["tradeup_completed"] = True
        entry["tradeup_completed_at"] = now
        for row in entry.get("materials") or []:
            if isinstance(row, dict):
                row.pop("inventory_missing_since", None)
    else:
        entry.pop("tradeup_completed", None)
        entry.pop("tradeup_completed_at", None)
    batch["updated_at"] = now
    _write_batch(path, batch)
    return True


def purchase_batch_recipe_tradeup_readiness(entry: dict[str, Any]) -> tuple[bool, str]:
    """Return whether an entry can be sent to the local CS2 trade-up executor."""
    if bool(entry.get("tradeup_completed")):
        return False, "该配方已标记为已汰换"
    materials = [row for row in entry.get("materials") or [] if isinstance(row, dict)]
    material_count = len(materials)
    if material_count not in (5, 10):
        return False, "一键汰换仅支持 10 件材料或 5 件隐秘级材料的配方"
    if material_count == 5:
        recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
        substrates = recipe.get("substrates_display") or []
        for material in materials:
            try:
                substrate = substrates[int(material.get("substrate_index"))]
            except (IndexError, TypeError, ValueError):
                substrate = {}
            replacement = material.get("replacement")
            name = str(
                (replacement if isinstance(replacement, dict) else {}).get("name")
                or (substrate if isinstance(substrate, dict) else {}).get("name")
                or material.get("name")
                or ""
            )
            template = get_template_from_goods_name(name)
            if template is None or tradeup_display_quality(template) != "隐秘":
                return False, "五合一仅支持 5 件隐秘级材料"
    if any(str(row.get("status") or "") != STATUS_RECEIVED for row in materials):
        return False, f"配方的 {material_count} 件材料全部入库后才能一键汰换"
    if any(row.get("inventory_missing_since") for row in materials):
        return False, "有材料已离开库存，请先完成离库确认"
    if any(row.get("normal_departure_assetid") for row in materials):
        return False, "该配方包含已经正常离库的材料"
    asset_ids = [str(row.get("matched_assetid") or "") for row in materials]
    if any(not asset_id for asset_id in asset_ids):
        return False, "有材料缺少 Steam 资产 ID，请重新核对库存"
    if len(set(asset_ids)) != len(asset_ids):
        return False, "配方中存在重复的 Steam 资产 ID"
    return True, "材料齐全，可以一键汰换"


def inventory_item_tradeup_cd_readiness(
    item: dict[str, Any],
    *,
    now: datetime,
) -> tuple[bool, str]:
    """Check the trade-hold fields persisted by the Steam inventory pipeline."""
    raw_ends_at = item.get("cooldown_ends_at")
    if raw_ends_at not in (None, ""):
        try:
            ends_at = datetime.fromtimestamp(float(raw_ends_at), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return False, "材料的 CD 时间无效，请刷新 Steam 库存"
        if now.astimezone(timezone.utc) < ends_at:
            local = ends_at.astimezone()
            return False, f"材料仍在 CD 中，至 {local:%Y-%m-%d %H:%M}"
    # Old inventory caches have no marketable/cooldown fields.  Preserve their
    # compatibility; a current cache that explicitly says it is cooling is not
    # allowed through merely because its timestamp was missing.
    if (
        "marketable" in item
        and not bool(item.get("marketable"))
        and (
            str(item.get("cooldown_kind") or "") == "trade_hold"
            or str(item.get("steam_inventory_context_id") or "") == "16"
        )
        and raw_ends_at in (None, "")
    ):
        return False, "材料仍在 CD 中，请刷新 Steam 库存后重试"
    if str(item.get("cooldown_kind") or "") == "market_listed":
        return False, "材料仍在 Steam 市场挂售，请先下架并刷新库存"
    return True, "CD 已结束"


def purchase_batch_recipe_live_readiness(
    batch: dict[str, Any],
    entry: dict[str, Any],
    inventory_items: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate received state, exact live assets, and every material's CD."""
    ready, reason = purchase_batch_recipe_tradeup_readiness(entry)
    if not ready:
        return ready, reason
    materials = [row for row in entry.get("materials") or [] if isinstance(row, dict)]
    inventory_by_id = {
        str(item.get("assetid") or ""): item
        for item in inventory_items
        if isinstance(item, dict) and str(item.get("assetid") or "")
    }
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.astimezone()
    for index, material in enumerate(materials, start=1):
        asset_id = str(material.get("matched_assetid") or "")
        item = inventory_by_id.get(asset_id)
        if item is None:
            return False, f"第 {index} 件材料已不在当前 Steam 库存，请先刷新库存"
        cd_ready, cd_reason = inventory_item_tradeup_cd_readiness(item, now=current)
        if not cd_ready:
            return False, f"第 {index} 件{cd_reason}"
    return True, f"已核对 {len(materials)} 件库存材料，CD 均已结束"


def list_ready_purchase_batch_recipes(
    profile_id: str = "",
    *,
    now: datetime | None = None,
) -> list[tuple[Path, dict[str, Any], list[str]]]:
    """List fully received batches with the recipe ids currently safe to craft."""
    wanted_profile = str(profile_id or "").strip()
    results: list[tuple[Path, dict[str, Any], list[str]]] = []
    inventory_cache: dict[str, list[dict[str, Any]]] = {}
    for path, batch in list_purchase_batches():
        if purchase_batch_section(batch) != "purchase_completed":
            continue
        batch_profile = str(batch.get("profile_id") or "")
        if wanted_profile and batch_profile != wanted_profile:
            continue
        inventory = inventory_cache.setdefault(
            batch_profile, load_profile_inventory_items(batch_profile)
        )
        ready_ids = [
            str(entry.get("id") or "")
            for entry in batch.get("recipes") or []
            if isinstance(entry, dict)
            and purchase_batch_recipe_live_readiness(
                batch, entry, inventory, now=now
            )[0]
        ]
        ready_ids = [value for value in ready_ids if value]
        if ready_ids:
            results.append((path, batch, ready_ids))
    return results


def list_tradeup_records(profile_id: str = "") -> list[dict[str, Any]]:
    """Build account-aware craft history, resolving output ids from cached inventory."""
    wanted_profile = str(profile_id or "").strip()
    inventory_cache: dict[str, dict[str, dict[str, Any]]] = {}
    records: list[dict[str, Any]] = []
    try:
        archived = json.loads(_tradeup_history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        archived = []
    if isinstance(archived, list):
        records.extend(
            copy.deepcopy(row)
            for row in archived
            if isinstance(row, dict)
            and (not wanted_profile or str(row.get("profile_id") or "") == wanted_profile)
        )
    for path, batch in list_purchase_batches():
        batch_profile = str(batch.get("profile_id") or "")
        if wanted_profile and batch_profile != wanted_profile:
            continue
        if batch_profile not in inventory_cache:
            inventory_cache[batch_profile] = {
                str(item.get("assetid") or ""): item
                for item in load_profile_inventory_items(batch_profile)
                if isinstance(item, dict) and str(item.get("assetid") or "")
            }
        inventory_by_id = inventory_cache[batch_profile]
        for recipe_index, entry in enumerate(batch.get("recipes") or [], start=1):
            if not isinstance(entry, dict) or not bool(entry.get("tradeup_completed")):
                continue
            execution = entry.get("tradeup_execution")
            execution = execution if isinstance(execution, dict) else {}
            recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
            materials = [
                copy.deepcopy(row)
                for row in execution.get("materials") or []
                if isinstance(row, dict)
            ]
            if not materials:
                substrates = recipe.get("substrates_display") or []
                for row in entry.get("materials") or []:
                    if not isinstance(row, dict):
                        continue
                    try:
                        substrate = substrates[int(row.get("substrate_index"))]
                    except (IndexError, TypeError, ValueError):
                        substrate = {}
                    substrate = substrate if isinstance(substrate, dict) else {}
                    materials.append(
                        {
                            "asset_id": str(row.get("matched_assetid") or ""),
                            "name": str(substrate.get("name") or row.get("name") or "未知材料"),
                            "float_value": row.get("matched_float", row.get("float_value")),
                            "price": row.get("price", substrate.get("price")),
                        }
                    )
            output_ids = [str(value) for value in execution.get("output_asset_ids") or []]
            stored_product_rows = [
                copy.deepcopy(row)
                for row in execution.get("products") or []
                if isinstance(row, dict)
            ]
            stored_products = {
                str(row.get("asset_id") or ""): copy.deepcopy(row)
                for row in stored_product_rows
                if isinstance(row, dict) and str(row.get("asset_id") or "")
            }
            products: list[dict[str, Any]] = []
            for asset_id in output_ids:
                item = inventory_by_id.get(asset_id, {})
                raw_price = item.get("buff_price", item.get("price"))
                try:
                    price = float(raw_price)
                    if not math.isfinite(price) or price <= 0:
                        price = None
                except (TypeError, ValueError):
                    price = None
                products.append(
                    {
                        "asset_id": asset_id,
                        "name": str(
                            item.get("market_name")
                            or item.get("name")
                            or item.get("market_hash_name")
                            or stored_products.get(asset_id, {}).get("name")
                            or f"产物（资产 ID …{asset_id[-6:]}）"
                        ),
                        "float_value": item.get(
                            "float", item.get(
                                "float_value", stored_products.get(asset_id, {}).get("float_value")
                            )
                        ),
                        "price": price if price is not None else stored_products.get(asset_id, {}).get("price"),
                        "steam_icon_url": str(
                            item.get("icon_url")
                            or stored_products.get(asset_id, {}).get("steam_icon_url")
                            or ""
                        ),
                        "source": str(
                            stored_products.get(asset_id, {}).get("source")
                            or ("current_inventory" if item else "")
                        ),
                    }
                )
            # History-recovered/manual records may not have an output asset ID.
            # Their persisted product snapshot is still authoritative and must
            # be displayed instead of being discarded by the output-id loop.
            output_id_set = set(output_ids)
            products.extend(
                row
                for row in stored_product_rows
                if not str(row.get("asset_id") or "")
                or str(row.get("asset_id") or "") not in output_id_set
            )
            raw_cost = execution.get("material_cost")
            if raw_cost is None:
                try:
                    prices = [float(row.get("price")) for row in materials]
                    raw_cost = sum(prices) if len(prices) == len(materials) else None
                except (TypeError, ValueError):
                    raw_cost = None
            product_prices = [row.get("price") for row in products]
            output_value = (
                sum(float(value) for value in product_prices)
                if products and all(value is not None for value in product_prices)
                else None
            )
            profit = (
                float(output_value) - float(raw_cost)
                if output_value is not None and raw_cost is not None
                else None
            )
            records.append(
                {
                    "batch_path": str(path),
                    "batch_name": str(batch.get("name") or "未命名采购批次"),
                    "recipe_entry_id": str(entry.get("id") or ""),
                    "recipe_index": recipe_index,
                    "recipe_title": str(entry.get("title") or "采购配方"),
                    "target_paint_index": str(
                        (recipe.get("special_wear_target") or {}).get("paint_index")
                        if isinstance(recipe.get("special_wear_target"), dict)
                        else ""
                    ),
                    "profile_id": batch_profile,
                    "account_name": str(batch.get("account_name") or "Steam"),
                    "completed_at": str(
                        execution.get("completed_at")
                        or entry.get("tradeup_completed_at")
                        or batch.get("updated_at")
                        or ""
                    ),
                    "materials": materials,
                    "products": products,
                    "product_candidates": copy.deepcopy(
                        [
                            row
                            for row in (
                                execution.get("product_candidates")
                                or recipe.get("products_display")
                                or []
                            )
                            if isinstance(row, dict)
                        ]
                    ),
                    "material_cost": raw_cost,
                    "output_value": output_value,
                    "profit": profit,
                }
            )
    # Archived records keep asset ids.  Re-resolve them on every view so a
    # later Steam inventory refresh can fill the actual product name/price.
    for record in records:
        record_profile = str(record.get("profile_id") or "")
        if record_profile not in inventory_cache:
            inventory_cache[record_profile] = {
                str(item.get("assetid") or ""): item
                for item in load_profile_inventory_items(record_profile)
                if isinstance(item, dict) and str(item.get("assetid") or "")
            }
        by_id = inventory_cache[record_profile]
        for product in record.get("products") or []:
            if not isinstance(product, dict):
                continue
            item = by_id.get(str(product.get("asset_id") or ""))
            if not item:
                continue
            product["name"] = str(
                item.get("market_name") or item.get("name")
                or item.get("market_hash_name") or product.get("name") or "未知产物"
            )
            product["float_value"] = item.get("float", item.get("float_value"))
            try:
                price = float(item.get("buff_price", item.get("price")))
                product["price"] = price if math.isfinite(price) and price > 0 else None
            except (TypeError, ValueError):
                product["price"] = None
        product_rows = [row for row in record.get("products") or [] if isinstance(row, dict)]
        prices = [row.get("price") for row in product_rows]
        output_value = (
            sum(float(value) for value in prices)
            if product_rows and all(value is not None for value in prices)
            else None
        )
        record["output_value"] = output_value
        record["profit"] = (
            output_value - float(record["material_cost"])
            if output_value is not None and record.get("material_cost") is not None
            else None
        )
    records.sort(key=lambda row: str(row.get("completed_at") or ""), reverse=True)
    # Older archived rows predate recipe_index.  Give every row a stable,
    # human-readable number within its batch instead of repeating its generic
    # saved title (for example, “炼金计算方案 01”).
    by_batch: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_batch.setdefault(str(record.get("batch_path") or record.get("batch_name") or ""), []).append(record)
    for batch_records in by_batch.values():
        chronological = sorted(
            batch_records,
            key=lambda row: (str(row.get("completed_at") or ""), str(row.get("recipe_entry_id") or "")),
        )
        for fallback_index, record in enumerate(chronological, start=1):
            if not record.get("recipe_index"):
                record["recipe_index"] = fallback_index
    return records


def _record_material_signature(record: dict[str, Any]) -> Counter[str]:
    return Counter(
        key
        for row in record.get("materials") or []
        if isinstance(row, dict)
        for key in [recipe_substrate_template_key({"name": row.get("name")})]
        if key
    )


def _history_event_input_signature(event: dict[str, Any]) -> Counter[str]:
    return Counter(
        key
        for row in event.get("inputs") or []
        if isinstance(row, dict)
        for key in [inventory_item_template_key(row)]
        if key
    )


def _history_product_for_record(
    record: dict[str, Any],
    history_item: dict[str, Any],
) -> dict[str, Any]:
    template = resolve_inventory_skin_template(history_item)
    template_key = inventory_item_template_key(history_item)
    chosen: dict[str, Any] = {}
    for candidate in record.get("product_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if recipe_substrate_template_key({"name": candidate.get("name")}) == template_key:
            chosen = candidate
            break
    current_products = [row for row in record.get("products") or [] if isinstance(row, dict)]
    asset_id = str(current_products[0].get("asset_id") or "") if current_products else ""
    if str(chosen.get("name") or "").strip():
        name = str(chosen["name"])
    elif template is not None:
        name = (
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name else template.weapon_name
        )
    else:
        name = str(
            history_item.get("market_name") or history_item.get("name")
            or history_item.get("market_hash_name") or "未知产物"
        )
    return {
        "asset_id": asset_id,
        "name": name,
        "float_value": chosen.get("float_value"),
        "price": chosen.get("price"),
        "steam_icon_url": str(history_item.get("icon_url") or ""),
        "source": "steam_inventory_history",
    }


def apply_steam_tradeup_history(
    profile_id: str,
    events: list[dict[str, Any]],
) -> int:
    """Match Steam Crafted rows to local recipes and persist the actual output."""
    profile_id = str(profile_id or "").strip()
    records = list_tradeup_records(profile_id)
    event_rows = [event for event in events if isinstance(event, dict)]
    used_events: set[int] = set()
    updates: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if any(
            isinstance(product, dict)
            and str(product.get("source") or "") == "steam_inventory_history"
            for product in record.get("products") or []
        ):
            continue
        signature = _record_material_signature(record)
        if not signature:
            continue
        match_index = next(
            (
                index for index, event in enumerate(event_rows)
                if index not in used_events
                and _history_event_input_signature(event) == signature
                and any(isinstance(row, dict) for row in event.get("outputs") or [])
            ),
            None,
        )
        if match_index is None:
            continue
        used_events.add(match_index)
        history_item = next(
            row for row in event_rows[match_index].get("outputs") or [] if isinstance(row, dict)
        )
        product = _history_product_for_record(record, history_item)
        updates[(str(record.get("batch_path") or ""), str(record.get("recipe_entry_id") or ""))] = product

    if not updates:
        return 0
    changed = 0
    for path, batch in list_purchase_batches():
        dirty = False
        for entry in batch.get("recipes") or []:
            if not isinstance(entry, dict):
                continue
            key = (str(path), str(entry.get("id") or ""))
            product = updates.get(key)
            execution = entry.get("tradeup_execution")
            if product is None:
                continue
            if not isinstance(execution, dict):
                execution = {
                    "method": "steam_inventory_history_recovery",
                    "completed_at": str(
                        entry.get("tradeup_completed_at")
                        or batch.get("updated_at")
                        or _utc_now()
                    ),
                    "output_asset_ids": [],
                }
                recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
                execution["product_candidates"] = copy.deepcopy(
                    [
                        row for row in recipe.get("products_display") or []
                        if isinstance(row, dict)
                    ]
                )
                entry["tradeup_execution"] = execution
            execution["products"] = [copy.deepcopy(product)]
            execution["history_synced_at"] = _utc_now()
            dirty = True
            changed += 1
        if dirty:
            batch["updated_at"] = _utc_now()
            _write_batch(path, batch)

    history_path = _tradeup_history_path()
    try:
        archived = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        archived = []
    archive_dirty = False
    if isinstance(archived, list):
        for record in archived:
            if not isinstance(record, dict):
                continue
            key = (str(record.get("batch_path") or ""), str(record.get("recipe_entry_id") or ""))
            product = updates.get(key)
            if product is None:
                continue
            record["products"] = [copy.deepcopy(product)]
            prices = [product.get("price")]
            record["output_value"] = float(prices[0]) if prices[0] is not None else None
            record["profit"] = (
                float(prices[0]) - float(record["material_cost"])
                if prices[0] is not None and record.get("material_cost") is not None else None
            )
            record["history_synced_at"] = _utc_now()
            archive_dirty = True
            changed += 1
    if archive_dirty:
        temporary = history_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(history_path)
    return changed


def delete_tradeup_completed_batches(profile_id: str = "") -> int:
    """Clear completed batches after preserving their statistics history."""
    wanted_profile = str(profile_id or "").strip()
    paths = [
        path
        for path, batch in list_purchase_batches()
        if purchase_batch_is_tradeup_completed(batch)
        and (not wanted_profile or str(batch.get("profile_id") or "") == wanted_profile)
    ]
    path_keys = {str(path) for path in paths}
    records_to_archive = [
        row for row in list_tradeup_records(wanted_profile)
        if str(row.get("batch_path") or "") in path_keys
    ]
    if records_to_archive:
        history_path = _tradeup_history_path()
        try:
            existing = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing = []
        if not isinstance(existing, list):
            existing = []
        known = {
            (str(row.get("batch_path") or ""), str(row.get("recipe_entry_id") or ""))
            for row in existing if isinstance(row, dict)
        }
        existing.extend(
            copy.deepcopy(row)
            for row in records_to_archive
            if (str(row.get("batch_path") or ""), str(row.get("recipe_entry_id") or "")) not in known
        )
        history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = history_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(history_path)
    for path in paths:
        delete_purchase_batch(path)
    return len(paths)


def _tradeup_history_path() -> Path:
    """Keep history beside the batch directory so patched/test stores stay isolated."""
    return PURCHASE_BATCHES_DIR.parent / "tradeup_history.json"


def build_purchase_batch_recipe_tradeup_plan(
    path: Path,
    recipe_entry_id: str,
) -> dict[str, Any]:
    """Build a frozen, user-confirmable plan for the local GC executor."""
    batch = load_purchase_batch(path)
    entry = next(
        (
            candidate
            for candidate in batch.get("recipes") or []
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == str(recipe_entry_id or "")
        ),
        None,
    )
    if entry is None:
        raise ValueError("未找到要汰换的配方")
    ready, reason = purchase_batch_recipe_tradeup_readiness(entry)
    if not ready:
        raise ValueError(reason)
    profile_id = str(batch.get("profile_id") or "").strip()
    steam_id = str(batch.get("steam_id") or "").strip()
    if not profile_id or not steam_id:
        raise ValueError("采购批次尚未绑定有效的 Steam 账号")
    recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
    substrates = recipe.get("substrates_display") or []
    materials: list[dict[str, Any]] = []
    for row in entry.get("materials") or []:
        try:
            substrate = substrates[int(row.get("substrate_index"))]
        except (IndexError, TypeError, ValueError):
            substrate = {}
        materials.append(
            {
                "asset_id": str(row.get("matched_assetid") or ""),
                "name": str(
                    (substrate if isinstance(substrate, dict) else {}).get("name")
                    or row.get("name")
                    or "未知材料"
                ),
                "float_value": float(row.get("matched_float") or row.get("float_value") or 0),
            }
        )
    return {
        "batch_path": str(Path(path).resolve()),
        "recipe_entry_id": str(recipe_entry_id),
        "title": str(entry.get("title") or "采购配方"),
        "profile_id": profile_id,
        "steam_id": steam_id,
        "asset_ids": [item["asset_id"] for item in materials],
        "materials": materials,
    }


def resolve_steam_tradeup_products(
    output_items: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Turn the GC's immediate output payload into display-ready product rows."""
    try:
        price_map = try_build_product_price_map_from_disk()
    except Exception:
        price_map = None
    pid_map = get_pid_map()
    products: list[dict[str, Any]] = []
    for output_item in output_items or []:
        if not isinstance(output_item, dict):
            continue
        asset_id = str(
            output_item.get("assetId") or output_item.get("asset_id") or ""
        )
        try:
            paint_index = str(int(float(output_item.get("paintIndex"))))
        except (TypeError, ValueError, OverflowError):
            paint_index = ""
        try:
            float_value = float(output_item.get("paintWear"))
            if not math.isfinite(float_value):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            float_value = None
        template = pid_map.get(paint_index) if paint_index else None
        name = ""
        price = None
        if template is not None:
            name = (
                f"{template.weapon_name} | {template.skin_name}"
                if template.skin_name else template.weapon_name
            )
            if float_value is not None:
                appearance = SkinInstance.get_appearance(float_value)
                if template.skin_name and appearance:
                    name = f"{name}（{appearance}）"
                if price_map:
                    price = lookup_template_price_value(
                        template, float_value, price_map
                    )
        products.append(
            {
                "asset_id": asset_id,
                "paint_index": paint_index,
                "name": name or f"产物（资产 ID …{asset_id[-6:]}）",
                "float_value": float_value,
                "price": price,
                "source": "steam_gc",
            }
        )
    return products


def record_purchase_batch_recipe_tradeup_result(
    path: Path,
    recipe_entry_id: str,
    *,
    input_asset_ids: list[str],
    output_asset_ids: list[str],
    output_items: list[dict[str, Any]] | None = None,
    gc_recipe: int,
) -> None:
    """Atomically record a successful GC craft without accepting a stale plan."""
    batch = load_purchase_batch(path)
    entry = next(
        (
            candidate
            for candidate in batch.get("recipes") or []
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == str(recipe_entry_id or "")
        ),
        None,
    )
    if entry is None:
        raise ValueError("汰换已完成，但采购配方记录已不存在")
    current_ids = [
        str(row.get("matched_assetid") or "")
        for row in entry.get("materials") or []
        if isinstance(row, dict)
    ]
    expected_ids = [str(value) for value in input_asset_ids]
    if current_ids != expected_ids:
        raise ValueError("汰换已完成，但配方材料记录在执行期间发生了变化")
    now = _utc_now()
    recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
    substrates = recipe.get("substrates_display") or []
    material_snapshots: list[dict[str, Any]] = []
    material_cost = 0.0
    material_prices_complete = True
    for row in entry.get("materials") or []:
        if not isinstance(row, dict):
            continue
        try:
            substrate = substrates[int(row.get("substrate_index"))]
        except (IndexError, TypeError, ValueError):
            substrate = {}
        substrate = substrate if isinstance(substrate, dict) else {}
        replacement = row.get("replacement")
        replacement = replacement if isinstance(replacement, dict) else {}
        raw_price = replacement.get("purchase_price", row.get("price", substrate.get("price")))
        try:
            price = float(raw_price)
            if not math.isfinite(price) or price < 0:
                raise ValueError
            material_cost += price
        except (TypeError, ValueError):
            price = None
            material_prices_complete = False
        material_snapshots.append(
            {
                "asset_id": str(row.get("matched_assetid") or ""),
                "name": str(replacement.get("name") or substrate.get("name") or row.get("name") or "未知材料"),
                "float_value": float(row.get("matched_float") or substrate.get("float_value") or row.get("float_value") or 0),
                "price": price,
            }
        )
    product_snapshots = resolve_steam_tradeup_products(output_items)
    entry["tradeup_completed"] = True
    entry["tradeup_completed_at"] = now
    entry["tradeup_execution"] = {
        "method": "local_steam_gc",
        "gc_recipe": int(gc_recipe),
        "input_asset_ids": expected_ids,
        "output_asset_ids": [str(value) for value in output_asset_ids if str(value)],
        "products": product_snapshots,
        "completed_at": now,
        "materials": material_snapshots,
        "material_cost": material_cost if material_prices_complete else None,
        "product_candidates": copy.deepcopy(
            [row for row in recipe.get("products_display") or [] if isinstance(row, dict)]
        ),
    }
    for row in entry.get("materials") or []:
        if isinstance(row, dict):
            row.pop("inventory_missing_since", None)
    batch["updated_at"] = now
    _write_batch(path, batch)


def record_inventory_recipe_tradeup_result(
    plan: dict[str, Any],
    *,
    output_asset_ids: list[str],
    output_items: list[dict[str, Any]] | None = None,
    gc_recipe: int,
) -> dict[str, Any]:
    """Persist a successful inventory-recipe GC craft into tradeup history."""
    profile_id = str(plan.get("profile_id") or "").strip()
    if not profile_id:
        raise ValueError("汰换已完成，但配方缺少本地 Steam 账号")
    input_ids = [str(value or "").strip() for value in plan.get("asset_ids") or []]
    if len(input_ids) not in (5, 10) or any(not value for value in input_ids):
        raise ValueError("汰换已完成，但材料资产编号无效")
    if len(set(input_ids)) != len(input_ids):
        raise ValueError("汰换已完成，但材料资产编号存在重复")

    now = _utc_now()
    plan_materials = [
        row for row in plan.get("materials") or [] if isinstance(row, dict)
    ]
    materials_by_id = {
        str(row.get("asset_id") or ""): row for row in plan_materials
    }
    material_snapshots: list[dict[str, Any]] = []
    for asset_id in input_ids:
        row = materials_by_id.get(asset_id, {})
        raw_price = row.get("price")
        try:
            price = float(raw_price)
            if not math.isfinite(price) or price < 0:
                price = None
        except (TypeError, ValueError):
            price = None
        material_snapshots.append(
            {
                "asset_id": asset_id,
                "name": str(row.get("name") or "未知材料"),
                "float_value": float(row.get("float_value") or 0),
                "price": price,
            }
        )
    material_prices = [row.get("price") for row in material_snapshots]
    material_cost = (
        sum(float(value) for value in material_prices)
        if material_prices and all(value is not None for value in material_prices)
        else None
    )
    product_snapshots = resolve_steam_tradeup_products(output_items)
    account_name = "Steam"
    for entry in list_profile_entries():
        if str(entry.get("id") or "") == profile_id:
            account_name = combo_display_name_for_profile(entry)
            break
    recipe_entry_id = f"inventory-{uuid.uuid4().hex}"
    title = str(plan.get("title") or "Steam 库存配方").strip() or "Steam 库存配方"
    record = {
        "batch_path": f"inventory:{profile_id}",
        "batch_name": "Steam 库存配方",
        "recipe_entry_id": recipe_entry_id,
        "recipe_index": 1,
        "recipe_title": title,
        "source": "steam_inventory_recipe",
        "profile_id": profile_id,
        "account_name": account_name,
        "steam_id": str(plan.get("steam_id") or ""),
        "completed_at": now,
        "materials": material_snapshots,
        "products": product_snapshots,
        "product_candidates": [],
        "material_cost": material_cost,
        "output_value": None,
        "profit": None,
        "method": "local_steam_gc",
        "gc_recipe": int(gc_recipe),
        "input_asset_ids": input_ids,
        "output_asset_ids": [str(value) for value in output_asset_ids if str(value)],
    }
    product_prices = [row.get("price") for row in product_snapshots if isinstance(row, dict)]
    if product_snapshots and all(value is not None for value in product_prices):
        output_value = sum(float(value) for value in product_prices)
        record["output_value"] = output_value
        if material_cost is not None:
            record["profit"] = output_value - float(material_cost)

    history_path = _tradeup_history_path()
    try:
        existing = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append(copy.deepcopy(record))
    history_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = history_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(history_path)
    return record


def set_purchase_batch_material_status(
    path: Path,
    recipe_entry_id: str,
    row_id: str,
    status: str,
) -> bool:
    if status not in VALID_STATUSES:
        return False
    batch = load_purchase_batch(path)
    found = _find_material(batch, recipe_entry_id, row_id)
    if found is None:
        return False
    entry, row = found
    old_status = str(row.get("status") or STATUS_PENDING)
    if old_status == status:
        return False
    old_assetid = str(row.get("matched_assetid") or "")
    row["status"] = status
    if old_status == STATUS_RECEIVED and status != STATUS_RECEIVED:
        if old_assetid:
            ignored = row.setdefault("ignored_asset_ids", [])
            if isinstance(ignored, list) and old_assetid not in ignored:
                ignored.append(old_assetid)
        _restore_departed_item_in_recipe(entry, row)
        batch.pop("alchemy_ready_at", None)
    if status == STATUS_ORDERED:
        # A non-received row being marked ordered is an explicit new purchase
        # attempt.  Assets ignored by an earlier "undo received" must become
        # eligible again, while RECEIVED -> ORDERED keeps the anti-bounce guard.
        if old_status != STATUS_RECEIVED:
            row.pop("ignored_asset_ids", None)
        row["ordered_at"] = row.get("ordered_at") or _utc_now()
        row.pop("matched_assetid", None)
        row.pop("matched_float", None)
        row.pop("received_at", None)
    elif status == STATUS_RECEIVED:
        row["received_at"] = row.get("received_at") or _utc_now()
    else:
        row.pop("matched_assetid", None)
        row.pop("matched_float", None)
        row.pop("received_at", None)
        if status == STATUS_PENDING:
            if old_status == STATUS_CANCELLED:
                row.pop("ignored_asset_ids", None)
            row.pop("ordered_at", None)
            row.pop("replacement", None)
    for key in (
        "inventory_missing_since",
        "normal_departure_assetid",
        "normal_departure_at",
    ):
        row.pop(key, None)
    batch["updated_at"] = _utc_now()
    _write_batch(path, batch)
    return True


def mark_all_purchase_batch_materials_ordered(path: Path) -> int:
    batch = load_purchase_batch(path)
    changed = 0
    now = _utc_now()
    for entry in batch.get("recipes") or []:
        if not isinstance(entry, dict):
            continue
        for row in entry.get("materials") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or STATUS_PENDING) != STATUS_PENDING:
                continue
            row["status"] = STATUS_ORDERED
            row["ordered_at"] = now
            row.pop("ignored_asset_ids", None)
            changed += 1
    if changed:
        batch["updated_at"] = now
        _write_batch(path, batch)
    return changed


def toggle_all_purchase_batch_materials_ordered(path: Path) -> tuple[int, str]:
    """Mark every non-received row ordered, or undo when all are already ordered."""
    batch = load_purchase_batch(path)
    rows = [
        row
        for entry in batch.get("recipes") or []
        if isinstance(entry, dict)
        for row in entry.get("materials") or []
        if isinstance(row, dict)
        and str(row.get("status") or STATUS_PENDING) != STATUS_RECEIVED
    ]
    if not rows:
        return 0, STATUS_ORDERED
    undo = all(
        str(row.get("status") or STATUS_PENDING) == STATUS_ORDERED
        for row in rows
    )
    target = STATUS_PENDING if undo else STATUS_ORDERED
    now = _utc_now()
    changed = 0
    for row in rows:
        old_status = str(row.get("status") or STATUS_PENDING)
        if old_status == target:
            continue
        row["status"] = target
        if target == STATUS_ORDERED:
            row["ordered_at"] = row.get("ordered_at") or now
            row.pop("ignored_asset_ids", None)
        else:
            row.pop("ordered_at", None)
            row.pop("matched_assetid", None)
            row.pop("matched_float", None)
            row.pop("received_at", None)
        changed += 1
    if changed:
        batch["updated_at"] = now
        _write_batch(path, batch)
    return changed, target


def update_purchase_batch_account(
    path: Path,
    *,
    profile_id: str,
    steam_id: str,
    account_name: str,
    inventory_items: list[dict[str, Any]],
) -> int:
    """Rebind a batch and reset inventory matches that belonged to the old account."""
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("Steam 收货账号不能为空")
    batch = load_purchase_batch(path)
    reset_received = 0
    for entry in batch.get("recipes") or []:
        if not isinstance(entry, dict):
            continue
        for row in entry.get("materials") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or STATUS_PENDING) == STATUS_RECEIVED:
                _restore_departed_item_in_recipe(entry, row)
                row["status"] = (
                    STATUS_ORDERED if row.get("ordered_at") else STATUS_PENDING
                )
                reset_received += 1
            row.pop("matched_assetid", None)
            row.pop("matched_float", None)
            row.pop("received_at", None)
            row.pop("inventory_missing_since", None)
            row.pop("normal_departure_assetid", None)
            row.pop("normal_departure_at", None)
    now = _utc_now()
    batch["profile_id"] = profile_id
    batch["steam_id"] = str(steam_id or "").strip()
    batch["account_name"] = str(account_name or "").strip() or "Steam"
    batch["baseline_asset_ids"] = sorted(
        {
            str(item.get("assetid") or "")
            for item in inventory_items
            if str(item.get("assetid") or "")
        }
    )
    batch.pop("alchemy_ready_at", None)
    batch["updated_at"] = now
    _write_batch(path, batch)
    return reset_received


def apply_purchase_batch_replacement(
    path: Path,
    recipe_entry_id: str,
    row_id: str,
    option: dict[str, Any],
) -> None:
    if not bool(option.get("safe")):
        raise ValueError("该候选会改变产物池，必须重新计算配方后才能采用")
    try:
        purchase_price = float(option.get("purchase_price"))
    except (TypeError, ValueError):
        raise ValueError("请填写替代材料的购买价格") from None
    if not math.isfinite(purchase_price) or purchase_price <= 0:
        raise ValueError("替代材料购买价格必须大于0")
    if option.get("manual_wear") is None:
        raise ValueError("请填写替代材料实际磨损的小数点后前6位")
    try:
        manual_wear = float(option.get("manual_wear"))
        manual_decimals = int(option.get("manual_wear_decimals"))
    except (TypeError, ValueError):
        raise ValueError("替代材料实际磨损前6位无效") from None
    if manual_decimals != 6 or not math.isfinite(manual_wear):
        raise ValueError("替代材料实际磨损必须严格填写小数点后前6位")
    allowed_min = float(option.get("allowed_min_wear", option.get("min_wear")))
    allowed_max = float(option.get("allowed_max_wear", option.get("max_wear")))
    prefix_min = manual_wear
    prefix_max = (
        1.0
        if manual_wear == 1.0
        else math.nextafter(manual_wear + 0.000001, -math.inf)
    )
    matched_min = max(allowed_min, prefix_min)
    matched_max = min(allowed_max, prefix_max)
    if matched_min > matched_max:
        raise ValueError("填写的磨损前6位不在所选材料允许购买区间内")
    batch = load_purchase_batch(path)
    found = _find_material(batch, recipe_entry_id, row_id)
    if found is None:
        raise ValueError("未找到要替代的材料槽位")
    _entry, row = found
    row["replacement"] = {
        "name": str(option.get("name") or ""),
        "template_key": str(option.get("template_key") or ""),
        "min_wear": matched_min,
        "max_wear": matched_max,
        "relation": str(option.get("relation") or "安全替代"),
        "purchase_price": purchase_price,
    }
    if option.get("target_avg_nfv") is not None:
        row["replacement"]["target_avg_nfv"] = float(option["target_avg_nfv"])
    row["replacement"]["manual_wear"] = manual_wear
    row["replacement"]["manual_wear_decimals"] = 6
    row["replacement"]["manual_wear_match_mode"] = "decimal_prefix_6"
    row["replacement"]["allowed_min_wear"] = allowed_min
    row["replacement"]["allowed_max_wear"] = allowed_max
    row["status"] = STATUS_PENDING
    # Choosing a replacement starts a fresh purchase attempt.  A matching
    # Steam asset may legitimately be the same asset that was undone earlier.
    row.pop("ignored_asset_ids", None)
    row.pop("ordered_at", None)
    row.pop("matched_assetid", None)
    row.pop("matched_float", None)
    row.pop("received_at", None)
    batch["updated_at"] = _utc_now()
    _write_batch(path, batch)


def _parse_special_target(recipe: dict[str, Any]) -> tuple[str, float, float] | None:
    target = recipe.get("special_wear_target")
    if isinstance(target, dict):
        try:
            paint_index = str(target.get("paint_index") or "")
            parsed = (
                paint_index,
                float(target.get("min_wear")),
                float(target.get("max_wear")),
            )
            if paint_index:
                return parsed
        except (TypeError, ValueError):
            pass
    raw = str(recipe.get("special_wear_input") or "")
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[0], float(parts[1]), float(parts[2])
    except (TypeError, ValueError):
        return None


def _outcome_signature(template: SkinTemplate) -> str:
    try:
        members = sorted(
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in template.upper_skins
        )
        return json.dumps(members, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(template.upper_skins)


def _replacement_wear_interval(
    recipe: dict[str, Any],
    substrate_index: int,
    candidate: SkinTemplate,
    output_template: SkinTemplate,
    target_low: float,
    target_high: float,
) -> tuple[float, float] | None:
    substrates = recipe.get("substrates_display")
    if not isinstance(substrates, list) or not (0 <= substrate_index < len(substrates)):
        return None
    resolved: list[tuple[SkinTemplate, float]] = []
    for index, row in enumerate(substrates):
        if not isinstance(row, dict):
            return None
        template = candidate if index == substrate_index else get_template_from_goods_name(
            str(row.get("name") or "")
        )
        if template is None:
            return None
        try:
            wear = float(row.get("float_value"))
        except (TypeError, ValueError):
            return None
        resolved.append((template, wear))

    def output_at(wear: float) -> float:
        values = [
            (template, wear if index == substrate_index else original_wear)
            for index, (template, original_wear) in enumerate(resolved)
        ]
        average = tradeup_average_normalized_float32(values)
        return tradeup_product_wear_float32(average, output_template)

    minimum = float(candidate.min_float)
    maximum = float(candidate.max_float)
    if maximum < minimum or output_at(maximum) < target_low or output_at(minimum) > target_high:
        return None

    low_a, low_b = minimum, maximum
    for _ in range(64):
        middle = (low_a + low_b) / 2.0
        if output_at(middle) < target_low:
            low_a = middle
        else:
            low_b = middle
    high_a, high_b = minimum, maximum
    for _ in range(64):
        middle = (high_a + high_b) / 2.0
        if output_at(middle) <= target_high:
            high_a = middle
        else:
            high_b = middle

    low = wear_as_float32(low_b)
    high = wear_as_float32(high_a)
    positive = np.float32(math.inf)
    negative = np.float32(-math.inf)
    for _ in range(8):
        if output_at(low) >= target_low:
            previous = float(np.nextafter(np.float32(low), negative))
            if previous < minimum or output_at(previous) < target_low:
                break
            low = previous
        else:
            low = float(np.nextafter(np.float32(low), positive))
    for _ in range(8):
        if output_at(high) <= target_high:
            following = float(np.nextafter(np.float32(high), positive))
            if following > maximum or output_at(following) > target_high:
                break
            high = following
        else:
            high = float(np.nextafter(np.float32(high), negative))
    low = max(minimum, float(low))
    high = min(maximum, float(high))
    if low > high or not (target_low <= output_at(low) <= target_high):
        return None
    if not (target_low <= output_at(high) <= target_high):
        return None
    return low, high


def _replacement_not_higher_interval(
    recipe: dict[str, Any],
    substrate_index: int,
    candidate: SkinTemplate,
    target_average: float,
) -> tuple[float, float] | None:
    """Candidate range whose normalized contribution does not exceed the missing item."""
    substrates = recipe.get("substrates_display")
    if not isinstance(substrates, list) or not (0 <= substrate_index < len(substrates)):
        return None
    original_source = substrates[substrate_index]
    if not isinstance(original_source, dict):
        return None
    original_template = get_template_from_goods_name(
        str(original_source.get("name") or "")
    )
    if original_template is None:
        return None
    try:
        original_wear = wear_as_float32(float(original_source.get("float_value")))
    except (TypeError, ValueError):
        return None
    original_normalized = SkinTemplate.float_to_normalized(
        original_wear,
        float(original_template.min_float),
        float(original_template.max_float),
    )
    corresponding_maximum = wear_as_float32(
        SkinTemplate.normalized_to_float(
            original_normalized,
            float(candidate.min_float),
            float(candidate.max_float),
        )
    )

    resolved: list[tuple[SkinTemplate, float]] = []
    for index, source in enumerate(substrates):
        if not isinstance(source, dict):
            return None
        template = candidate if index == substrate_index else get_template_from_goods_name(
            str(source.get("name") or "")
        )
        if template is None:
            return None
        try:
            wear = float(source.get("float_value"))
        except (TypeError, ValueError):
            return None
        resolved.append((template, wear))

    target = wear_as_float32(target_average)

    def average_at(wear: float) -> float:
        values = [
            (template, wear if index == substrate_index else original_wear)
            for index, (template, original_wear) in enumerate(resolved)
        ]
        return tradeup_average_normalized_float32(values)

    minimum = float(candidate.min_float)
    maximum = min(float(candidate.max_float), float(corresponding_maximum))
    if maximum < minimum or average_at(minimum) > target:
        return None
    low = float(candidate.min_float)
    high = wear_as_float32(maximum)
    negative = np.float32(-math.inf)
    for _ in range(8):
        if average_at(high) > target:
            high = float(np.nextafter(np.float32(high), negative))
        else:
            break
    low = max(minimum, float(low))
    high = min(maximum, float(high))
    if low > high or average_at(low) > target or average_at(high) > target:
        return None
    return low, high


def _recipe_average_float32(recipe: dict[str, Any]) -> float | None:
    pairs: list[tuple[SkinTemplate, float]] = []
    for source in recipe.get("substrates_display") or []:
        if not isinstance(source, dict):
            return None
        template = get_template_from_goods_name(str(source.get("name") or ""))
        if template is None:
            return None
        try:
            pairs.append((template, float(source.get("float_value"))))
        except (TypeError, ValueError):
            return None
    if not pairs:
        return None
    return tradeup_average_normalized_float32(pairs)


def purchase_batch_replacement_options(
    batch: dict[str, Any],
    recipe_entry_id: str,
    row_id: str,
) -> tuple[list[dict[str, Any]], str]:
    found = _find_material(batch, recipe_entry_id, row_id)
    if found is None:
        return [], "未找到缺口材料"
    entry, row = found
    recipe = entry.get("recipe")
    if not isinstance(recipe, dict):
        return [], "配方数据无效"
    target = _parse_special_target(recipe)
    output_template = None
    target_average = None
    if target is not None:
        target_pid, target_low, target_high = target
        output_template = get_pid_map().get(str(target_pid))
    else:
        target_average = _recipe_average_float32(recipe)
        if target_average is None:
            return [], "无法计算该配方的原始归一化磨损"
    try:
        substrate_index = int(row.get("substrate_index"))
        original = recipe["substrates_display"][substrate_index]
    except (KeyError, IndexError, TypeError, ValueError):
        return [], "缺口材料索引无效"
    original_template = get_template_from_goods_name(str(original.get("name") or ""))
    if original_template is None or (target is not None and output_template is None):
        return [], "无法识别原材料或目标产物模板"
    original_signature = _outcome_signature(original_template)
    unique: dict[tuple[str, bool], SkinTemplate] = {}
    for template in get_name_map().values():
        key = (str(template.paint_index), bool(template.stat_trak))
        unique.setdefault(key, template)

    options: list[dict[str, Any]] = []
    for template in unique.values():
        if template.quality != original_template.quality:
            continue
        if bool(template.stat_trak) != bool(original_template.stat_trak):
            continue
        same_template = (
            template.paint_index == original_template.paint_index
            and template.weapon_name == original_template.weapon_name
        )
        same_outcomes = _outcome_signature(template) == original_signature
        # A purchase-batch replacement must preserve the recipe's outcome pool.
        # Cross-pool templates are not actionable replacements, so do not expose
        # them even as reference rows in either normal or special-wear mode.
        if not (same_template or same_outcomes):
            continue
        if target is not None:
            interval = _replacement_wear_interval(
                recipe,
                substrate_index,
                template,
                output_template,
                target_low,
                target_high,
            )
        else:
            interval = _replacement_not_higher_interval(
                recipe,
                substrate_index,
                template,
                target_average,
            )
        if interval is None:
            continue
        relation = (
            "原材料"
            if same_template
            else "同产物池安全替代"
        )
        name = (
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        substrate = {"name": name, "float_value": sum(interval) / 2.0}
        options.append(
            {
                "name": name,
                "paint_index": str(template.paint_index),
                "template_key": recipe_substrate_template_key(substrate),
                "min_wear": interval[0],
                "max_wear": interval[1],
                "relation": relation,
                "safe": True,
                "original": bool(same_template),
                "target_avg_nfv": target_average,
                "range_mode": "target_interval" if target is not None else "not_higher",
            }
        )
    options.sort(
        key=lambda option: (
            bool(option.get("original")),
            str(option.get("name") or ""),
        )
    )
    if target is not None:
        target_text = f"目标产物磨损 {target_low:.18f} ～ {target_high:.18f}"
    else:
        target_text = (
            f"产物归一化磨损不高于原配方 {float(target_average):.18f}，且不改变产物池"
        )
    return options[:80], target_text


def _planned_template_key_from_row(row: dict[str, Any]) -> str:
    match_key = str(row.get("match_key") or "")
    if "|wear:" in match_key:
        return match_key.rsplit("|wear:", 1)[0]
    return recipe_substrate_template_key({"name": row.get("name")})


def _row_matches_inventory(row: dict[str, Any], item: dict[str, Any]) -> bool:
    assetid = str(item.get("assetid") or "")
    if assetid in {str(value) for value in row.get("ignored_asset_ids") or []}:
        return False
    replacement = row.get("replacement")
    if isinstance(replacement, dict):
        if inventory_item_template_key(item) != str(replacement.get("template_key") or ""):
            return False
        try:
            wear = wear_as_float32(float(item.get("float", item.get("float_value"))))
            return float(replacement.get("min_wear")) <= wear <= float(
                replacement.get("max_wear")
            )
        except (TypeError, ValueError, OverflowError):
            return False
    if inventory_item_match_key(item) == str(row.get("match_key") or ""):
        return True
    planned_template = _planned_template_key_from_row(row)
    if not planned_template or inventory_item_template_key(item) != planned_template:
        return False
    return inventory_wear_matches_planned(
        row.get("float_value"),
        item.get("float", item.get("float_value")),
    )


def _candidate_preserves_replacement_target(
    entry: dict[str, Any],
    row: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    replacement = row.get("replacement")
    if not isinstance(replacement, dict):
        return True
    recipe = entry.get("recipe")
    if not isinstance(recipe, dict):
        return False
    target = _parse_special_target(recipe)
    try:
        replacement_index = int(row.get("substrate_index"))
        candidate_wear = float(item.get("float", item.get("float_value")))
    except (TypeError, ValueError):
        return False
    pairs: list[tuple[SkinTemplate, float]] = []
    for index, source in enumerate(recipe.get("substrates_display") or []):
        if not isinstance(source, dict):
            return False
        name = (
            str(replacement.get("name") or "")
            if index == replacement_index
            else str(source.get("name") or "")
        )
        template = get_template_from_goods_name(name)
        if template is None:
            return False
        try:
            wear = candidate_wear if index == replacement_index else float(
                source.get("float_value")
            )
        except (TypeError, ValueError):
            return False
        pairs.append((template, wear))
    average = tradeup_average_normalized_float32(pairs)
    if target is None:
        try:
            expected_average = wear_as_float32(
                float(replacement.get("target_avg_nfv"))
            )
        except (TypeError, ValueError):
            return False
        return wear_as_float32(average) <= expected_average
    output_template = get_pid_map().get(str(target[0]))
    if output_template is None:
        return False
    output = tradeup_product_wear_float32(average, output_template)
    return float(target[1]) <= output <= float(target[2])


def _apply_received_item_to_recipe(
    entry: dict[str, Any],
    row: dict[str, Any],
    item: dict[str, Any],
) -> None:
    recipe = entry.get("recipe")
    try:
        substrate_index = int(row.get("substrate_index"))
        substrate = recipe["substrates_display"][substrate_index]
    except (KeyError, IndexError, TypeError, ValueError):
        return
    if not isinstance(row.get("pre_receive_substrate"), dict):
        row["pre_receive_substrate"] = copy.deepcopy(substrate)
    replacement = row.get("replacement")
    if isinstance(replacement, dict) and replacement.get("name"):
        substrate["name"] = str(replacement["name"])
    try:
        substrate["float_value"] = float(item.get("float", item.get("float_value")))
    except (TypeError, ValueError):
        pass
    substrate["platform"] = "steam_inventory"
    substrate["steam_assetid"] = str(item.get("assetid") or "")

    _refresh_recipe_wear_metrics(recipe)


def _refresh_recipe_wear_metrics(recipe: dict[str, Any]) -> None:
    pairs: list[tuple[SkinTemplate, float]] = []
    for source in recipe.get("substrates_display") or []:
        if not isinstance(source, dict):
            return
        template = get_template_from_goods_name(str(source.get("name") or ""))
        if template is None:
            return
        try:
            pairs.append((template, float(source.get("float_value"))))
        except (TypeError, ValueError):
            return
    if not pairs:
        return
    average = tradeup_average_normalized_float32(pairs)
    recipe["avg_nfv"] = float(average)
    target = _parse_special_target(recipe)
    if target is not None:
        output_template = get_pid_map().get(str(target[0]))
        if output_template is not None:
            recipe["special_wear_output_float"] = tradeup_product_wear_float32(
                average, output_template
            )


def _restore_departed_item_in_recipe(
    entry: dict[str, Any],
    row: dict[str, Any],
) -> None:
    recipe = entry.get("recipe")
    try:
        substrate_index = int(row.get("substrate_index"))
        substrate = recipe["substrates_display"][substrate_index]
    except (KeyError, IndexError, TypeError, ValueError):
        return
    snapshot = row.pop("pre_receive_substrate", None)
    if isinstance(snapshot, dict):
        substrate.clear()
        substrate.update(copy.deepcopy(snapshot))
    else:
        substrate["name"] = str(row.get("name") or substrate.get("name") or "")
        try:
            substrate["float_value"] = float(row.get("float_value"))
        except (TypeError, ValueError):
            pass
        substrate.pop("steam_assetid", None)
        if str(substrate.get("platform") or "") == "steam_inventory":
            substrate["platform"] = ""
    if isinstance(recipe, dict):
        _refresh_recipe_wear_metrics(recipe)


def resolve_purchase_batch_inventory_departure(
    path: Path,
    recipe_entry_id: str,
    row_id: str,
    *,
    seller_reversed: bool,
) -> bool:
    """Classify a received asset missing from inventory without guessing its cause."""
    batch = load_purchase_batch(path)
    found = _find_material(batch, recipe_entry_id, row_id)
    if found is None:
        return False
    entry, row = found
    if (
        str(row.get("status") or "") != STATUS_RECEIVED
        or not row.get("inventory_missing_since")
    ):
        return False
    now = _utc_now()
    assetid = str(row.get("matched_assetid") or "")
    row.pop("inventory_missing_since", None)
    if seller_reversed:
        if assetid:
            ignored = row.setdefault("ignored_asset_ids", [])
            if isinstance(ignored, list) and assetid not in ignored:
                ignored.append(assetid)
        row["status"] = STATUS_CANCELLED
        row["seller_reversed_at"] = now
        row.pop("matched_assetid", None)
        row.pop("matched_float", None)
        row.pop("received_at", None)
        row.pop("normal_departure_assetid", None)
        row.pop("normal_departure_at", None)
        _restore_departed_item_in_recipe(entry, row)
        batch.pop("alchemy_ready_at", None)
    else:
        row["normal_departure_assetid"] = assetid
        row["normal_departure_at"] = now
    batch["updated_at"] = now
    _write_batch(path, batch)
    return True


def reconcile_purchase_batches_for_profile(
    profile_id: str,
    inventory_items: list[dict[str, Any]],
    *,
    excluded_asset_ids: set[str] | None = None,
) -> dict[str, Any]:
    profile_id = str(profile_id or "").strip()
    entries = [
        (path, copy.deepcopy(batch))
        for path, batch in list_purchase_batches()
        if str(batch.get("profile_id") or "") == profile_id
    ]
    inventory_asset_ids = {
        str(item.get("assetid") or "")
        for item in inventory_items
        if isinstance(item, dict) and str(item.get("assetid") or "")
    }
    changed: set[Path] = set()
    used = {str(value) for value in (excluded_asset_ids or set()) if str(value)}
    for path, batch in entries:
        for recipe_entry in batch.get("recipes") or []:
            if not isinstance(recipe_entry, dict):
                continue
            tradeup_completed = bool(recipe_entry.get("tradeup_completed"))
            for row in recipe_entry.get("materials") or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") == STATUS_RECEIVED:
                    assetid = str(row.get("matched_assetid") or "")
                    if assetid:
                        used.add(assetid)
                    if tradeup_completed:
                        if row.pop("inventory_missing_since", None) is not None:
                            changed.add(path)
                            batch["updated_at"] = _utc_now()
                        continue
                    if assetid and assetid in inventory_asset_ids:
                        cleared = False
                        for key in (
                            "inventory_missing_since",
                            "normal_departure_assetid",
                            "normal_departure_at",
                        ):
                            if key in row:
                                row.pop(key, None)
                                cleared = True
                        if cleared:
                            changed.add(path)
                            batch["updated_at"] = _utc_now()
                    elif (
                        assetid
                        and str(row.get("normal_departure_assetid") or "") != assetid
                        and not row.get("inventory_missing_since")
                    ):
                        row["inventory_missing_since"] = _utc_now()
                        changed.add(path)
                        batch["updated_at"] = _utc_now()

    available = [
        item
        for item in inventory_items
        if isinstance(item, dict)
        and str(item.get("assetid") or "")
        and str(item.get("assetid") or "") not in used
    ]
    available.sort(key=lambda item: str(item.get("assetid") or ""))
    tasks: list[tuple[str, Path, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for path, batch in entries:
        for recipe_entry in batch.get("recipes") or []:
            if not isinstance(recipe_entry, dict):
                continue
            for row in recipe_entry.get("materials") or []:
                if isinstance(row, dict) and str(row.get("status") or STATUS_PENDING) == STATUS_ORDERED:
                    tasks.append(
                        (
                            str(row.get("ordered_at") or recipe_entry.get("added_at") or batch.get("created_at") or ""),
                            path,
                            batch,
                            recipe_entry,
                            row,
                        )
                    )
    tasks.sort(key=lambda task: (task[0], str(task[4].get("row_id") or "")))
    matched_by_path: dict[Path, int] = {}
    for _ordered_at, path, batch, recipe_entry, row in tasks:
        baseline = {str(value) for value in batch.get("baseline_asset_ids") or []}
        chosen = next(
            (
                item
                for item in available
                if str(item.get("assetid") or "") not in baseline
                and _row_matches_inventory(row, item)
                and _candidate_preserves_replacement_target(recipe_entry, row, item)
            ),
            None,
        )
        if chosen is None:
            continue
        available.remove(chosen)
        assetid = str(chosen.get("assetid") or "")
        used.add(assetid)
        row["status"] = STATUS_RECEIVED
        row["matched_assetid"] = assetid
        row["matched_float"] = float(chosen.get("float", chosen.get("float_value", 0.0)))
        row["received_at"] = _utc_now()
        _apply_received_item_to_recipe(recipe_entry, row, chosen)
        batch["updated_at"] = _utc_now()
        changed.add(path)
        matched_by_path[path] = matched_by_path.get(path, 0) + 1

    matched = sum(matched_by_path.values())
    failures = 0
    failed_matches = 0
    for path, batch in entries:
        if path not in changed:
            continue
        try:
            _write_batch(path, batch)
        except (OSError, ValueError):
            failures += 1
            failed_matches += matched_by_path.get(path, 0)
    matched -= failed_matches
    waiting_by_path: dict[str, int] = {}
    for path, batch in entries:
        waiting_by_path[str(path.resolve())] = sum(
            1
            for recipe_entry in batch.get("recipes") or []
            if isinstance(recipe_entry, dict)
            for row in recipe_entry.get("materials") or []
            if isinstance(row, dict)
            and str(row.get("status") or STATUS_PENDING) == STATUS_ORDERED
        )
    waiting = sum(waiting_by_path.values()) + failed_matches
    missing_by_path: dict[str, int] = {}
    for path, batch in entries:
        missing_by_path[str(path.resolve())] = purchase_batch_summary(batch)[
            "missing_review"
        ]
    missing_review = sum(missing_by_path.values())
    matched_by_path_out = {
        str(path.resolve()): int(count)
        for path, count in matched_by_path.items()
    }
    return {
        "matched": matched,
        "waiting": waiting,
        "matched_by_path": matched_by_path_out,
        "waiting_by_path": waiting_by_path,
        "missing_review": missing_review,
        "missing_by_path": missing_by_path,
        "tracked_batches": len(entries),
        "save_failures": failures,
        "used_asset_ids": sorted(used),
    }


def reconcile_all_purchase_records_for_profile(
    profile_id: str,
    inventory_items: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy_used = received_asset_ids_for_saved_recipes(profile_id)
    batch_result = reconcile_purchase_batches_for_profile(
        profile_id,
        inventory_items,
        excluded_asset_ids=legacy_used,
    )
    batch_used = {str(value) for value in batch_result.get("used_asset_ids") or []}
    legacy_result = reconcile_saved_recipes_for_profile(
        profile_id,
        inventory_items,
        excluded_asset_ids=batch_used,
    )
    return {
        "matched": int(batch_result.get("matched") or 0)
        + int(legacy_result.get("matched") or 0),
        "waiting": int(batch_result.get("waiting") or 0)
        + int(legacy_result.get("waiting") or 0),
        "matched_by_path": dict(batch_result.get("matched_by_path") or {}),
        "waiting_by_path": dict(batch_result.get("waiting_by_path") or {}),
        "missing_review": int(batch_result.get("missing_review") or 0),
        "missing_by_path": dict(batch_result.get("missing_by_path") or {}),
        "save_failures": int(batch_result.get("save_failures") or 0)
        + int(legacy_result.get("save_failures") or 0),
        "tracked_batches": int(batch_result.get("tracked_batches") or 0),
        "tracked_recipes": int(legacy_result.get("tracked_recipes") or 0),
    }
