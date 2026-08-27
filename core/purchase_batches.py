"""Batch-level purchasing, Steam reconciliation, and replacement planning."""

from __future__ import annotations

import copy
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from config import PURCHASE_BATCHES_DIR
from core.alchemy_calc import (
    tradeup_average_normalized_float32,
    tradeup_product_wear_float32,
)
from core.alchemy_quality import get_name_map, get_pid_map, get_template_from_goods_name
from core.data_utils import SkinTemplate, wear_as_float32
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
    return counts


def purchase_batch_is_fully_received(batch: dict[str, Any]) -> bool:
    summary = purchase_batch_summary(batch)
    return summary["total"] > 0 and summary[STATUS_RECEIVED] == summary["total"]


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
    _entry, row = found
    old_status = str(row.get("status") or STATUS_PENDING)
    if old_status == status:
        return False
    old_assetid = str(row.get("matched_assetid") or "")
    row["status"] = status
    if status == STATUS_ORDERED:
        row["ordered_at"] = row.get("ordered_at") or _utc_now()
        if old_status == STATUS_RECEIVED and old_assetid:
            ignored = row.setdefault("ignored_asset_ids", [])
            if isinstance(ignored, list) and old_assetid not in ignored:
                ignored.append(old_assetid)
        row.pop("matched_assetid", None)
        row.pop("received_at", None)
    elif status == STATUS_RECEIVED:
        row["received_at"] = row.get("received_at") or _utc_now()
    else:
        row.pop("matched_assetid", None)
        row.pop("received_at", None)
        if status == STATUS_PENDING:
            row.pop("ordered_at", None)
            row.pop("replacement", None)
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
                row["status"] = (
                    STATUS_ORDERED if row.get("ordered_at") else STATUS_PENDING
                )
                reset_received += 1
            row.pop("matched_assetid", None)
            row.pop("matched_float", None)
            row.pop("received_at", None)
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
    row.pop("ordered_at", None)
    row.pop("matched_assetid", None)
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
    replacement = row.get("replacement")
    if isinstance(replacement, dict) and replacement.get("name"):
        substrate["name"] = str(replacement["name"])
    try:
        substrate["float_value"] = float(item.get("float", item.get("float_value")))
    except (TypeError, ValueError):
        pass
    substrate["platform"] = "steam_inventory"
    substrate["steam_assetid"] = str(item.get("assetid") or "")

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
    used = {str(value) for value in (excluded_asset_ids or set()) if str(value)}
    for _path, batch in entries:
        for recipe_entry in batch.get("recipes") or []:
            if not isinstance(recipe_entry, dict):
                continue
            for row in recipe_entry.get("materials") or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") == STATUS_RECEIVED:
                    assetid = str(row.get("matched_assetid") or "")
                    if assetid:
                        used.add(assetid)

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
    changed: set[Path] = set()
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
    matched_by_path_out = {
        str(path.resolve()): int(count)
        for path, count in matched_by_path.items()
    }
    return {
        "matched": matched,
        "waiting": waiting,
        "matched_by_path": matched_by_path_out,
        "waiting_by_path": waiting_by_path,
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
        "save_failures": int(batch_result.get("save_failures") or 0)
        + int(legacy_result.get("save_failures") or 0),
        "tracked_batches": int(batch_result.get("tracked_batches") or 0),
        "tracked_recipes": int(legacy_result.get("tracked_recipes") or 0),
    }
