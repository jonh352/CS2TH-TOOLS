"""炼金计算核心 - 产物价格（分 paint_index）、均价期望、Dinkelbach、配方汇总与保本率"""

import bisect
from collections import Counter
import json
import logging
import math
import multiprocessing
import time
import numpy as np
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Union

import requests

from config import (
    ALCHEMY_RESULT_DISPLAY_TOP_N,
    ALCHEMY_SCAN_MODE_DINKELBACH_TOPK,
    ALCHEMY_TARGET_MODE_DINKELBACH_TOPK,
    APP_VERSION,
    LOCAL_PRODUCT_PRICE_SNAPSHOT,
    PRICE_API_ENABLED,
    PRODUCT_PRICE_API,
    PRODUCT_PRICE_FILE,
    PRODUCT_PRICE_HTTP_META_FILE,
    SOFTWARE_LOGIN_PRODUCT_PRICE_ERROR_MESSAGE,
)
from .auth_client import AuthClient
from .alchemy_quality import (
    canonical_goods_name_for_lookup,
    get_pid_map,
    get_template_from_goods_name,
    resolve_inventory_skin_template,
)
from .data_utils import (
    QUALITY_MAP,
    SkinInstance,
    SkinTemplate,
    UPPER_QUALITY_MAP,
    wear_as_float32,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300  # 5 分钟内使用本地缓存，与解密后的 fetch_time 对齐


class ComputationCancelled(Exception):
    """用户请求中断计算（由 cancel_check 或 UI 线程 requestInterruption 配合触发）。"""


def _product_price_cache_is_fresh(data: dict, now: datetime) -> bool:
    """与明文缓存时代一致：payload 内 fetch_time 为 %Y%m%d_%H%M%S，未过期则 True。"""
    fetch_time_str = data.get("fetch_time", "")
    if not fetch_time_str or not isinstance(fetch_time_str, str):
        return False
    try:
        fetch_dt = datetime.strptime(fetch_time_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return False
    return now - fetch_dt <= timedelta(seconds=CACHE_SECONDS)


def _validate_product_price_payload(data: dict, source: str) -> None:
    if "ordinary" not in data and "stat_trak" not in data:
        raise RuntimeError(f"产物价格缺少 ordinary/stat_trak ({source})")


def _load_product_price_cache() -> dict | None:
    try:
        data = json.loads(PRODUCT_PRICE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_product_price_cache(data: dict) -> bool:
    try:
        PRODUCT_PRICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = PRODUCT_PRICE_FILE.with_suffix(PRODUCT_PRICE_FILE.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temp.replace(PRODUCT_PRICE_FILE)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _load_product_price_http_meta() -> dict:
    try:
        data = json.loads(PRODUCT_PRICE_HTTP_META_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_product_price_http_meta(etag: str) -> None:
    try:
        PRODUCT_PRICE_HTTP_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = PRODUCT_PRICE_HTTP_META_FILE.with_suffix(
            PRODUCT_PRICE_HTTP_META_FILE.suffix + ".tmp"
        )
        temp.write_text(
            json.dumps(
                {"etag": etag, "checked_at": time.time()},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temp.replace(PRODUCT_PRICE_HTTP_META_FILE)
    except OSError:
        logger.warning("无法保存价格同步元数据")


def _http_price_cache_is_fresh(meta: dict) -> bool:
    try:
        return time.time() - float(meta.get("checked_at") or 0) <= CACHE_SECONDS
    except (TypeError, ValueError):
        return False


def _fetch_product_price_from_api(
    access_token: str,
    etag: str = "",
) -> tuple[dict | None, str, bool]:
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-CS2TH-Client": "cs2th-tools",
            "X-CS2TH-Version": APP_VERSION,
            "Accept-Encoding": "gzip",
        }
        if etag:
            headers["If-None-Match"] = etag
        resp = requests.get(
            PRODUCT_PRICE_API,
            headers=headers,
            timeout=30,
        )
        response_etag = str(resp.headers.get("ETag") or etag)
        if resp.status_code == 304:
            return None, response_etag, True
        data = resp.json() if resp.text else {}
        if not resp.ok:
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise RuntimeError(str(detail or f"价格服务请求失败（HTTP {resp.status_code}）"))
        if not isinstance(data, dict):
            raise RuntimeError("服务器返回的产物价格格式无效")
        _validate_product_price_payload(data, "服务器")
        return data, response_etag, False
    except requests.RequestException as e:
        raise RuntimeError(f"获取产物价格失败: {e}") from e


def load_product_price_raw(*, force_remote: bool = False) -> dict:
    """Prefer a local development snapshot, otherwise sync from CS2TH.

    ``force_remote`` bypasses the five-minute freshness shortcut and asks the
    server to validate the cached ETag.  It is used by the explicit inventory
    refresh action so the prices shown beside a newly fetched inventory are
    current at that moment.
    """
    now = datetime.now()
    data = _load_product_price_cache()
    http_meta = _load_product_price_http_meta()
    if (
        LOCAL_PRODUCT_PRICE_SNAPSHOT is not None
        and LOCAL_PRODUCT_PRICE_SNAPSHOT.is_file()
    ):
        try:
            from .product_price_sync import sync_product_price_cache

            data, changed = sync_product_price_cache(
                LOCAL_PRODUCT_PRICE_SNAPSHOT,
                PRODUCT_PRICE_FILE,
                data,
            )
            if changed:
                logger.info(
                    "已从本地 CS2TH 现货快照同步价格: %s",
                    LOCAL_PRODUCT_PRICE_SNAPSHOT,
                )
        except Exception as exc:
            # A snapshot may be in the middle of replacement. Existing cache
            # remains valid and the next calculation will retry automatically.
            logger.warning("本地 CS2TH 价格同步失败，继续使用已有缓存: %s", exc)
    if data is not None:
        try:
            _validate_product_price_payload(data, PRODUCT_PRICE_FILE.name)
            if not force_remote and (
                _http_price_cache_is_fresh(http_meta)
                or _product_price_cache_is_fresh(data, now)
            ):
                return data
        except RuntimeError:
            data = None

    if not PRICE_API_ENABLED:
        if data is not None and not force_remote:
            return data
        raise RuntimeError(SOFTWARE_LOGIN_PRODUCT_PRICE_ERROR_MESSAGE)
    session = AuthClient().load_local_session()
    if session is None:
        if data is not None and not force_remote:
            return data
        raise RuntimeError(SOFTWARE_LOGIN_PRODUCT_PRICE_ERROR_MESSAGE)
    try:
        raw, etag, not_modified = _fetch_product_price_from_api(
            session.access_token,
            str(http_meta.get("etag") or ""),
        )
    except RuntimeError:
        if data is not None and not force_remote:
            logger.warning("线上价格同步失败，继续使用本地缓存", exc_info=True)
            return data
        raise
    if not_modified:
        if data is None:
            raw, etag, _ = _fetch_product_price_from_api(session.access_token)
        else:
            _save_product_price_http_meta(etag)
            return data
    if raw is None:
        if data is not None:
            return data
        raise RuntimeError("服务器未返回价格数据")
    if not _save_product_price_cache(raw):
        logger.warning("产物价格已从服务器拉取，但写入本地缓存失败")
    _save_product_price_http_meta(etag)
    return raw


def _leaf_average(leaf: dict[str, float]) -> float:
    if not leaf:
        return 0.0
    vals = list(leaf.values())
    return sum(vals) / len(vals)


def build_price_map(raw_data: dict) -> dict:
    """解析为 price_map[stat_trak][box_id][quality][nfv]={paint_index: price}。算法侧期望用各档均价。"""
    result = {}
    for stat_trak in ("ordinary", "stat_trak"):
        if stat_trak not in raw_data:
            continue
        result[stat_trak] = {}
        box_data = raw_data[stat_trak]
        for box_id_str, quality_data in box_data.items():
            box_id = int(box_id_str)
            result[stat_trak][box_id] = {}
            for quality, nfv_data in quality_data.items():
                result[stat_trak][box_id][quality] = {}
                for nfv_str, leaf in nfv_data.items():
                    result[stat_trak][box_id][quality][float(nfv_str)] = leaf
    return result


def try_build_product_price_map_from_disk() -> dict | None:
    """Read the local JSON price cache without making a network request."""
    data = _load_product_price_cache()
    if data is None:
        return None
    try:
        _validate_product_price_payload(data, PRODUCT_PRICE_FILE.name)
    except RuntimeError:
        return None
    return build_price_map(data)


def _quality_nfv_leaves(
    price_map: dict, box_id: int, quality: str, stat_trak: bool
) -> dict[float, dict[str, float]]:
    """某箱、某中文品质、是否暗金下的 归一化磨损 -> 分 paint_index 价格表。"""
    key = "stat_trak" if stat_trak else "ordinary"
    if key not in price_map or box_id not in price_map[key]:
        return {}
    en_quality = QUALITY_MAP.get(quality, quality)
    return price_map[key][box_id].get(en_quality, {}) or {}


_price_lookup_cache_owner: dict | None = None
_expectation_map_cache: dict[tuple[int, str, bool], dict[float, float]] = {}
_product_nfv_keys_cache: dict[tuple[int, str, bool], list[float]] = {}


def _activate_price_lookup_cache(price_map: dict) -> None:
    """Keep derived price lookups only for the currently used price map.

    Product prices are immutable during one calculation.  Holding the owner by
    identity both prevents stale results when a fresh price package is loaded
    and prevents Python from reusing its ``id`` while cached entries remain.
    """

    global _price_lookup_cache_owner
    if _price_lookup_cache_owner is price_map:
        return
    _price_lookup_cache_owner = price_map
    _expectation_map_cache.clear()
    _product_nfv_keys_cache.clear()


def get_expectation_map(
    price_map: dict,
    box_id: int,
    quality: str,
    stat_trak: bool,
) -> dict:
    """归一化磨损 -> 均价（该档所有 paint_index 价的算术平均），供 assign_expectation / Dinkelbach。"""
    _activate_price_lookup_cache(price_map)
    cache_key = (int(box_id), str(quality), bool(stat_trak))
    cached = _expectation_map_cache.get(cache_key)
    if cached is not None:
        return cached
    leaves = _quality_nfv_leaves(price_map, box_id, quality, stat_trak)
    result = {
        nfv: _leaf_average({k: v for k, v in leaf.items() if not k.startswith("*")})
        for nfv, leaf in leaves.items()
    }
    _expectation_map_cache[cache_key] = result
    return result


def _sorted_product_nfvs(
    price_map: dict,
    box_id: int,
    quality: str,
    stat_trak: bool,
    nfv_map: dict[float, dict[str, float]],
) -> list[float]:
    """Return stable sorted product-price buckets for repeated UI valuation."""

    _activate_price_lookup_cache(price_map)
    cache_key = (int(box_id), str(quality), bool(stat_trak))
    cached = _product_nfv_keys_cache.get(cache_key)
    if cached is not None:
        return cached
    result = sorted(nfv_map.keys())
    _product_nfv_keys_cache[cache_key] = result
    return result


def _mapped_nfv_for_target(sorted_nfvs: list[float], target_nfv: float) -> float:
    """非空升序 NFV 列表上，与 assign_expectation / lookup 一致：第一个 >= target 的档，否则最大档。"""
    idx = bisect.bisect_left(sorted_nfvs, target_nfv)
    return sorted_nfvs[-1] if idx >= len(sorted_nfvs) else sorted_nfvs[idx]


def lookup_pid_price_at_nfv(
    price_map: dict,
    box_id: int,
    quality_zh: str,
    stat_trak: bool,
    target_nfv: float,
    paint_index: str,
) -> float:
    """与 assign_expectation 相同 NFV 分档规则，取某 paint_index 在该档的标价。无键或缺数据时 0。"""
    nfv_map = _quality_nfv_leaves(price_map, box_id, quality_zh, stat_trak)
    if not nfv_map:
        return 0.0
    all_nfvs = _sorted_product_nfvs(
        price_map,
        box_id,
        quality_zh,
        stat_trak,
        nfv_map,
    )
    mapped_nfv = _mapped_nfv_for_target(all_nfvs, target_nfv)
    leaf = nfv_map[mapped_nfv]
    ps = str(paint_index)
    if ps in leaf:
        return float(leaf[ps])
    if len(leaf) == 1 and "*" in leaf:
        return float(leaf["*"])
    return 0.0


def lookup_inventory_item_price_value(item: dict, price_map: dict | None) -> float | None:
    """库存物品在 price_map 中的有效单价；无图、无 float、无价或价为 0 时 None。"""
    if not price_map:
        return None
    tmpl = resolve_inventory_skin_template(item)
    if tmpl is None:
        return None
    fv = item.get("float")
    if fv is None:
        return None
    try:
        fv_f = float(fv)
    except (ValueError, TypeError):
        return None
    p = lookup_template_price_value(tmpl, fv_f, price_map)
    try:
        pf = float(p)
    except (TypeError, ValueError):
        return None
    if pf <= 0:
        return None
    return pf


def lookup_template_price_value(
    template: SkinTemplate,
    float_value: float,
    price_map: dict | None,
    weapon_box_id: int | None = None,
) -> float | None:
    """Look up a skin price, optionally constrained to the chosen collection."""
    if not price_map:
        return None
    try:
        nfv = SkinTemplate.float_to_normalized(
            float(float_value), template.min_float, template.max_float
        )
    except (TypeError, ValueError, AssertionError):
        return None
    box_ids = (
        [int(weapon_box_id)]
        if weapon_box_id is not None and int(weapon_box_id) > 0
        else [int(value) for value in template.weapon_box_id]
    )
    for box_id in box_ids:
        price = lookup_pid_price_at_nfv(
            price_map,
            box_id,
            template.quality,
            template.stat_trak,
            nfv,
            template.paint_index,
        )
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def apply_inventory_buff_prices(
    items: list[dict],
    price_map: dict | None,
    fetch_time: str = "",
) -> tuple[list[dict], int]:
    """Attach the current BUFF reference price to Steam inventory rows."""
    priced_items: list[dict] = []
    matched = 0
    for source in items:
        item = dict(source)
        price = lookup_inventory_item_price_value(item, price_map)
        item["buff_price"] = float(price) if price is not None else None
        item["buff_price_fetch_time"] = str(fetch_time or "")
        item["buff_price_source"] = "CS2TH"
        if price is not None:
            matched += 1
        priced_items.append(item)
    return priced_items, matched


def backfill_missing_substrate_prices(
    selected_data: list[dict],
    price_map: dict | None,
) -> tuple[list[dict], int, int]:
    """Fill missing/zero substrate prices after the latest price bundle is loaded.

    Positive prices, including values manually entered by the user, are kept.
    Fresh installations can import inventory before a desktop price bundle is
    cached, so those rows initially contain ``price=0``.
    """
    rows: list[dict] = []
    updated = 0
    unresolved = 0
    for source in selected_data:
        row = dict(source)
        try:
            current_price = float(row.get("price"))
        except (TypeError, ValueError):
            current_price = 0.0
        if math.isfinite(current_price) and current_price > 0:
            rows.append(row)
            continue

        template = get_template_from_goods_name(str(row.get("goods_name") or ""))
        try:
            float_value = float(row.get("float_value"))
        except (TypeError, ValueError):
            float_value = math.nan
        replacement = 0.0
        if template is not None and math.isfinite(float_value) and price_map:
            try:
                raw_box_id = row.get("weapon_box_id")
                box_id = int(raw_box_id) if raw_box_id not in (None, "") else None
                replacement = lookup_template_price_value(
                    template,
                    float_value,
                    price_map,
                    weapon_box_id=box_id,
                )
            except (TypeError, ValueError, AssertionError):
                replacement = 0.0
        try:
            replacement_value = float(replacement)
        except (TypeError, ValueError):
            replacement_value = 0.0
        if math.isfinite(replacement_value) and replacement_value > 0:
            row["price"] = replacement_value
            updated += 1
        else:
            row["price"] = 0.0
            unresolved += 1
        rows.append(row)
    return rows, updated, unresolved


def format_inventory_yuan_price(value: float | None) -> str:
    if value is None or value <= 0:
        return "￥-"
    s = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return f"￥{s}"


def _try_skin_instance_from_row(item: dict) -> SkinInstance | None:
    goods_name = item.get("goods_name", "")
    template = get_template_from_goods_name(goods_name)
    if not template:
        return None
    float_value = item.get("float_value")
    price = item.get("price")
    platform = item.get("platform", "buff")
    raw_pl = item.get("purchase_link")
    purchase_link = (
        raw_pl.strip()
        if isinstance(raw_pl, str) and raw_pl.strip()
        else None
    )
    if float_value is None or price is None:
        return None
    try:
        price_value = float(price)
        if not math.isfinite(price_value) or price_value <= 0:
            return None
        return SkinInstance(
            skin_template=template,
            float_value=float_value,
            price=price_value,
            platform=platform,
            purchase_link=purchase_link,
            steam_assetid=str(item.get("steam_assetid") or "") or None,
            steam_profile_id=str(item.get("steam_profile_id") or "") or None,
            steam_id=str(item.get("steam_id") or "") or None,
        )
    except (ValueError, AssertionError):
        return None


def _row_is_required(item: dict) -> bool:
    raw = item.get("must_select", False)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(raw)


def transform_to_instances(selected_data: list[dict]) -> list[SkinInstance]:
    """将勾选的底物数据转为 SkinInstance 列表。"""
    instances = []
    for item in selected_data:
        inst = _try_skin_instance_from_row(item)
        if inst is not None:
            instances.append(inst)
    return instances


def transform_to_instance_pairs(selected_data: list[dict]) -> list[tuple[dict, SkinInstance]]:
    """与 transform_to_instances 相同规则，但保留每条对应的原始 dict，供分层抽样等使用。"""
    pairs: list[tuple[dict, SkinInstance]] = []
    for item in selected_data:
        inst = _try_skin_instance_from_row(item)
        if inst is not None:
            pairs.append((item, inst))
    return pairs


def split_required_instance_pairs(
    selected_data: list[dict],
) -> tuple[list[tuple[dict, SkinInstance]], list[tuple[dict, SkinInstance]]]:
    """按 ``must_select`` 拆成 (必选, 可选) 两组，保持原始顺序。"""
    required_pairs: list[tuple[dict, SkinInstance]] = []
    optional_pairs: list[tuple[dict, SkinInstance]] = []
    for item in selected_data:
        inst = _try_skin_instance_from_row(item)
        if inst is None:
            continue
        if _row_is_required(item):
            required_pairs.append((item, inst))
        else:
            optional_pairs.append((item, inst))
    return required_pairs, optional_pairs


def partition_selected_data_by_tradeup_group(
    selected_data: list[dict],
    *,
    eligible_only: bool = True,
) -> list[tuple[str, bool, int, list[dict]]]:
    """按可实际炼金的 ``(品质, StatTrak)`` 拆分底物。

    无法解析或没有上级产物的行会被忽略。``eligible_only`` 为真时，再静默去掉
    有效数量不足该品质配方槽数（隐秘 5 件，其余 10 件）的组。
    返回项为 ``(品质, 是否 StatTrak, 槽数, 原始行列表)``。
    """
    grouped: dict[tuple[str, bool], list[dict]] = {}
    for row, instance in transform_to_instance_pairs(selected_data):
        template = instance.skin_template
        if not template.upper_skins:
            continue
        key = (template.quality, bool(template.stat_trak))
        grouped.setdefault(key, []).append(row)

    quality_rank = {quality: rank for rank, quality in enumerate(QUALITY_MAP)}
    result: list[tuple[str, bool, int, list[dict]]] = []
    for (quality, stat_trak), rows in sorted(
        grouped.items(),
        key=lambda item: (
            -quality_rank.get(item[0][0], -1),
            item[0][1],
        ),
    ):
        k = get_k_from_quality(quality)
        if eligible_only and len(rows) < k:
            continue
        result.append((quality, stat_trak, k, rows))
    return result


def eligible_selected_data_for_target(
    selected_data: list[dict],
    target_paint_index: str,
) -> list[dict]:
    """返回数量足够且能产出指定 paint_index 的单个品质/暗金组。"""
    target = str(target_paint_index)
    for _quality, _stat_trak, _k, rows in partition_selected_data_by_tradeup_group(
        selected_data,
        eligible_only=True,
    ):
        if any(
            target in {str(pid) for pid in instance.skin_template.upper_skins}
            for _row, instance in transform_to_instance_pairs(rows)
        ):
            return rows
    return []


def _build_sorted_nfv_cache(
    instances: list[SkinInstance],
    price_map: dict,
) -> dict[tuple, list[float]]:
    """预计算每个 (box_id, quality, stat_trak) 的 sorted NFV 列表，供 assign_expectation 复用。"""
    cache: dict[tuple, list[float]] = {}
    seen: set[tuple] = set()
    for inst in instances:
        box_id = inst.skin_template.weapon_box_id[0] if inst.skin_template.weapon_box_id else 0
        upper_quality = UPPER_QUALITY_MAP.get(inst.skin_template.quality, inst.skin_template.quality)
        stat_trak = inst.skin_template.stat_trak
        key = (box_id, upper_quality, stat_trak)
        if key in seen:
            continue
        seen.add(key)
        exp_map = get_expectation_map(price_map, box_id, upper_quality, stat_trak)
        cache[key] = sorted(exp_map.keys()) if exp_map else []
    return cache


def assign_expectation(
    instances: list[SkinInstance],
    nfv: float,
    k: int,
    price_map: dict,
    sorted_nfv_cache: Optional[dict[tuple, list[float]]] = None,
) -> None:
    """为每个 instance 设置 expectation。nfv 为目标归一化磨损，二分查找第一个 >= nfv 的价格。
    若提供 sorted_nfv_cache，则复用预计算的 NFV 列表，避免重复排序。"""
    for inst in instances:
        box_id = inst.skin_template.weapon_box_id[0] if inst.skin_template.weapon_box_id else 0
        upper_quality = UPPER_QUALITY_MAP.get(inst.skin_template.quality, inst.skin_template.quality)
        stat_trak = inst.skin_template.stat_trak
        exp_map = get_expectation_map(price_map, box_id, upper_quality, stat_trak)
        if not exp_map:
            inst.expectation = 0.0
            continue
        if sorted_nfv_cache is not None:
            key = (box_id, upper_quality, stat_trak)
            all_nfvs = sorted_nfv_cache[key]
        else:
            all_nfvs = sorted(exp_map.keys())
        if not all_nfvs:
            inst.expectation = 0.0
            continue
        mapped_nfv = _mapped_nfv_for_target(all_nfvs, nfv)
        inst.expectation = exp_map[mapped_nfv] / k


def find_all_nfv_in_range(
    instances: list[SkinInstance],
    price_map: dict,
    norm_min: float,
    norm_max: float,
) -> list[float]:
    """找出产物价格表中落在有效范围内的 nfv。
    有效范围 = 底物归一磨损范围 与 设置范围 的交集：
    最小取二者较大者，最大取二者较小者。
    """
    all_nfvs = set()
    for inst in instances:
        box_id = inst.skin_template.weapon_box_id[0] if inst.skin_template.weapon_box_id else 0
        upper_quality = UPPER_QUALITY_MAP.get(inst.skin_template.quality, inst.skin_template.quality)
        stat_trak = inst.skin_template.stat_trak
        exp_map = get_expectation_map(price_map, box_id, upper_quality, stat_trak)
        all_nfvs.update(exp_map.keys())
    substrate_min_nfv = min(inst.normalized_value for inst in instances)
    substrate_max_nfv = max(inst.normalized_value for inst in instances)
    effective_min = max(substrate_min_nfv, norm_min)
    effective_max = min(substrate_max_nfv, norm_max)
    nfvs_results = [nfv for nfv in all_nfvs if effective_min <= nfv <= effective_max]
    if effective_max not in all_nfvs:
        nfvs_results.append(effective_max)
    return sorted(nfvs_results)


def _fenwick_add(tree: list[int], i: int, delta: int) -> None:
    n = len(tree) - 1
    while i <= n:
        tree[i] += delta
        i += i & -i


def _fenwick_prefix(tree: list[int], i: int) -> int:
    s = 0
    while i > 0:
        s += tree[i]
        i -= i & -i
    return s


def update_dominance(substrates: list[SkinInstance], k: int) -> list[SkinInstance]:
    """k-支配过滤，返回 dominanced < k 的底物。"""
    for sub in substrates:
        sub.dominanced = 0
    if len(substrates) < 2:
        return [s for s in substrates if s.dominanced < k]
    nfv_sorted = sorted(set(s.normalized_value for s in substrates))
    nfv_to_rank = {v: i + 1 for i, v in enumerate(nfv_sorted)}
    sorted_subs = sorted(substrates, key=lambda s: (-s.value, s.normalized_value))
    fenwick = [0] * (len(nfv_sorted) + 1)
    for sub in sorted_subs:
        rank = nfv_to_rank[sub.normalized_value]
        sub.dominanced = _fenwick_prefix(fenwick, rank - 1)
        _fenwick_add(fenwick, rank, 1)
    return [s for s in substrates if s.dominanced < k]


def merge_node(A: list[tuple], B: list[tuple]) -> list[tuple]:
    """合并 Pareto 前沿。节点格式 (total_nfv, total_value, skin, parent)。"""
    merged = []
    idx_a = idx_b = 0
    len_a, len_b = len(A), len(B)
    max_val = -float("inf")
    while idx_a < len_a and idx_b < len_b:
        a, b = A[idx_a], B[idx_b]
        if a[0] < b[0]:
            if a[1] > max_val:
                merged.append(a)
                max_val = a[1]
            idx_a += 1
        elif a[0] > b[0]:
            if b[1] > max_val:
                merged.append(b)
                max_val = b[1]
            idx_b += 1
        else:
            if a[1] >= b[1]:
                if a[1] > max_val:
                    merged.append(a)
                    max_val = a[1]
            else:
                if b[1] > max_val:
                    merged.append(b)
                    max_val = b[1]
            idx_a += 1
            idx_b += 1
    while idx_a < len_a:
        a = A[idx_a]
        if a[1] > max_val:
            merged.append(a)
            max_val = a[1]
        idx_a += 1
    while idx_b < len_b:
        b = B[idx_b]
        if b[1] > max_val:
            merged.append(b)
            max_val = b[1]
        idx_b += 1
    return merged


def _reconstruct_solution(tail: tuple) -> list[SkinInstance]:
    """从 dp 节点回溯重构底物列表"""
    solution = []
    while tail[2] is not None:
        solution.append(tail[2])
        tail = tail[3]
    return solution[::-1]


def sort_substrate(substrates: list[SkinInstance]) -> list[SkinInstance]:
    """按性价比从高到低返回新列表。"""
    pos = [s for s in substrates if s.value >= 0]
    neg = [s for s in substrates if s.value < 0]
    pos.sort(
        key=lambda s: (
            s.value / max(s.normalized_value, 1e-12),
            -s.normalized_value,
            s.value,
        ),
        reverse=True,
    )
    neg.sort(
        key=lambda s: (
            s.value,                # 越大越好：-0.1 比 -1 好
            -s.normalized_value,    # 磨损越小越好
        ),
        reverse=True,
    )
    return pos + neg

def sort_solution(substrates: list[SkinInstance]) -> list[SkinInstance]:
    """按性价比从高到低返回新列表。"""
    pos = [s for s in substrates if s.value >= 0]
    neg = [s for s in substrates if s.value < 0]
    pos.sort(
        key=lambda s: (
            s.value / max(s.normalized_value, 1e-12),
            -s.normalized_value,
            s.value,
        ),
        reverse=True,
    )
    neg.sort(
        key=lambda s: (
            -s.normalized_value,    # 磨损越小越好
            s.value,                # 越大越好：-0.1 比 -1 好  
        ),
        reverse=True,
    )
    return pos + neg


def _suffix_min_nfv_sums(vals: list[float], k: int) -> list[list[float]]:
    """suffix_min[p][r] = 下标严格大于 p 的底物中 r 件最小归一化磨损之和（0/1 多重集）。

    与 backpack 顺序一致：处理到下标 p 时，未来只能从 p+1..n-1 再选；尚需 r 件则至少再增加
    本行之和。不可行（后缀件数 < r）为 inf。时间 O(n·k)，空间 O(n·k)；仅维护全局 (k-1) 小。
    """
    n = len(vals)
    INF = float("inf")
    suffix_min: list[list[float]] = [[0.0] * k for _ in range(n)]
    if n == 0 or k <= 1:
        return suffix_min
    small: list[float] = []
    for p in range(n - 1, -1, -1):
        need = n - 1 - p
        row = suffix_min[p]
        acc = 0.0
        for r in range(1, k):
            if need < r:
                row[r] = INF
            else:
                acc += small[r - 1]
                row[r] = acc
        if p > 0:
            bisect.insort(small, vals[p])
            if len(small) > k - 1:
                small.pop()
    return suffix_min


def backpack(
    substrates: list[SkinInstance],
    k: int,
    max_nfv: float,
    timeout: float = -1,
    return_topk: int = 1,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[list[list[SkinInstance]]]:
    """背包求解，返回至多 return_topk 组最优底物列表。"""
    start_time = time.time()
    max_nfv_total = max_nfv * k
    vals = [s.normalized_value for s in substrates]
    suffix_min = _suffix_min_nfv_sums(vals, k)
    dp = {i: [] for i in range(k)}
    dp[-1] = [(0.0, 0.0, None, None)]
    for p, sub in enumerate(substrates):
        if cancel_check is not None and cancel_check():
            raise ComputationCancelled
        if timeout > 0 and time.time() - start_time > timeout:
            break
        snfv = sub.normalized_value
        sval = sub.value
        for i in range(k - 1, -1, -1):
            if not dp[i - 1]:
                continue
            r = k - i - 1
            if r > 0 and math.isinf(suffix_min[p][r]):
                continue
            tail_min = 0.0 if r == 0 else suffix_min[p][r]
            new_list = []
            for node in dp[i - 1]:
                new_nfv = node[0] + snfv
                if new_nfv >= max_nfv_total:
                    break
                if r > 0 and new_nfv + tail_min >= max_nfv_total:
                    break
                new_list.append((new_nfv, node[1] + sval, sub, node))
            if new_list:
                if not dp[i]:
                    dp[i] = new_list
                else:
                    dp[i] = merge_node(dp[i], new_list)
    if not dp[k - 1]:
        return None
    # return _pick_backpack_solutions_float32_valid(dp[k - 1], k, max_nfv, return_topk)
    return [_reconstruct_solution(sol) for sol in dp[k - 1][-return_topk:]]


_CONSTRAINED_MIX_MAX_PATTERNS = 4096
_CONSTRAINED_MIX_MAX_FRONT_NODES = 96


def _outcome_mix_signature(substrate: SkinInstance) -> tuple[str, ...]:
    """A collection/outcome identity used to preserve break-even diversity."""
    return tuple(sorted(str(pid) for pid in substrate.skin_template.upper_skins or []))


def _update_dominance_per_outcome_mix(
    substrates: list[SkinInstance],
    k: int,
) -> list[SkinInstance]:
    """Apply dominance inside each outcome group instead of across groups.

    Cross-group dominance is valid for a pure ROI objective, but it can erase an
    expensive collection whose product probabilities are required by a
    break-even constraint.
    """
    grouped: dict[tuple[str, ...], list[SkinInstance]] = {}
    for substrate in substrates:
        grouped.setdefault(_outcome_mix_signature(substrate), []).append(substrate)
    filtered: list[SkinInstance] = []
    for rows in grouped.values():
        filtered.extend(update_dominance(rows, k))
    return filtered


def _trim_mix_front(nodes: list[tuple]) -> list[tuple]:
    """Bound a per-mix Pareto front while retaining its full NFV span."""
    limit = _CONSTRAINED_MIX_MAX_FRONT_NODES
    if len(nodes) <= limit:
        return nodes
    indexes = {
        round(index * (len(nodes) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [nodes[index] for index in sorted(indexes)]


def _trim_mix_patterns(
    fronts: dict[tuple[int, ...], list[tuple]],
) -> dict[tuple[int, ...], list[tuple]]:
    """Keep objective leaders plus evenly spread collection-count patterns."""
    limit = _CONSTRAINED_MIX_MAX_PATTERNS
    if len(fronts) <= limit:
        return fronts
    ranked = sorted(
        fronts,
        key=lambda key: fronts[key][-1][1] if fronts[key] else -float("inf"),
        reverse=True,
    )
    keep = set(ranked[: limit // 2])
    ordered = sorted(fronts)
    sample_count = limit - len(keep)
    if sample_count > 0:
        for index in range(sample_count):
            position = round(index * (len(ordered) - 1) / max(1, sample_count - 1))
            keep.add(ordered[position])
            if len(keep) >= limit:
                break
    if len(keep) < limit:
        keep.update(key for key in ranked if key not in keep and len(keep) < limit)
    return {key: fronts[key] for key in keep}


def backpack_preserving_outcome_mix(
    substrates: list[SkinInstance],
    k: int,
    max_nfv: float,
    timeout: float = -1,
    return_topk_per_mix: int = 3,
    cancel_check: Optional[Callable[[], bool]] = None,
    solution_accepts: Optional[Callable[[list[SkinInstance]], bool]] = None,
) -> list[list[SkinInstance]]:
    """Search Pareto fronts separately for each collection-count pattern.

    A recipe's collection-count pattern determines its product probabilities.
    Keeping those patterns separate prevents the unconstrained ROI winner from
    deleting all low/high break-even candidates before the requested range is
    evaluated.
    """
    if k <= 0 or len(substrates) < k:
        return []
    signatures = sorted({_outcome_mix_signature(sub) for sub in substrates})
    group_index = {signature: index for index, signature in enumerate(signatures)}
    zero_mix = (0,) * len(signatures)
    start_time = time.time()
    max_nfv_total = max_nfv * k
    vals = [s.normalized_value for s in substrates]
    suffix_min = _suffix_min_nfv_sums(vals, k)
    fronts_by_count: list[dict[tuple[int, ...], list[tuple]]] = [
        {} for _ in range(k)
    ]
    base_fronts = {zero_mix: [(0.0, 0.0, None, None)]}

    for p, substrate in enumerate(substrates):
        if cancel_check is not None and cancel_check():
            raise ComputationCancelled
        if timeout > 0 and time.time() - start_time > timeout:
            break
        substrate_group = group_index[_outcome_mix_signature(substrate)]
        snfv = substrate.normalized_value
        sval = substrate.value
        for count_index in range(k - 1, -1, -1):
            previous = (
                base_fronts
                if count_index == 0
                else fronts_by_count[count_index - 1]
            )
            if not previous:
                continue
            remaining = k - count_index - 1
            if remaining > 0 and math.isinf(suffix_min[p][remaining]):
                continue
            tail_min = 0.0 if remaining == 0 else suffix_min[p][remaining]
            current = fronts_by_count[count_index]
            for mix, nodes in list(previous.items()):
                next_mix_list = list(mix)
                next_mix_list[substrate_group] += 1
                next_mix = tuple(next_mix_list)
                additions = []
                for node in nodes:
                    new_nfv = node[0] + snfv
                    if new_nfv >= max_nfv_total:
                        break
                    if remaining > 0 and new_nfv + tail_min >= max_nfv_total:
                        break
                    additions.append((new_nfv, node[1] + sval, substrate, node))
                if not additions:
                    continue
                existing = current.get(next_mix)
                merged = additions if not existing else merge_node(existing, additions)
                current[next_mix] = _trim_mix_front(merged)
            if len(current) > _CONSTRAINED_MIX_MAX_PATTERNS * 2:
                fronts_by_count[count_index] = _trim_mix_patterns(current)

    final_fronts = _trim_mix_patterns(fronts_by_count[k - 1])
    accepted: list[tuple[tuple, list[SkinInstance]]] = []
    per_mix = max(1, int(return_topk_per_mix))
    for nodes in final_fronts.values():
        mix_count = 0
        for tail in reversed(nodes):
            solution = _reconstruct_solution(tail)
            if solution_accepts is not None and not solution_accepts(solution):
                continue
            accepted.append((tail, solution))
            mix_count += 1
            if mix_count >= per_mix:
                break
    accepted.sort(key=lambda pair: pair[0][1])
    return [solution for _tail, solution in accepted]

def dinkelbach(
    substrates: list[SkinInstance],
    k: int,
    max_nfv: float,
    timeout: float = -1,
    return_topk: int = 1,
    cancel_check: Optional[Callable[[], bool]] = None,
    required_substrates: Optional[list[SkinInstance]] = None,
    preserve_outcome_mix: bool = False,
    break_even_range: tuple[float, float] | None = None,
    break_even_price_map: dict | None = None,
) -> Union[Optional[dict], list[dict]]:
    """Dinkelbach 迭代求最大收益率配方。
    return_topk>0 时，收敛后从 dp[k-1] 取 topk 个解按收益率排序返回。"""
    required = list(required_substrates or [])
    optional_k = k - len(required)
    fixed_cost = sum(s.price for s in required)
    fixed_expectation = sum(s.expectation for s in required)
    fixed_nfv_total = sum(s.normalized_value for s in required)
    if optional_k < 0:
        return []
    if optional_k == 0:
        if fixed_cost <= 0 or not required:
            return []
        solution = sort_solution(required)
        nfvs = [s.normalized_value for s in solution]
        avg_nfv = sum(nfvs) / len(nfvs)
        return [{
            "solution": solution,
            "rate": fixed_expectation / fixed_cost - 1,
            "cost": fixed_cost,
            "expectation": fixed_expectation,
            "avg_nfv": avg_nfv,
        }]
    residual_nfv_total = max_nfv * k - fixed_nfv_total
    if residual_nfv_total <= 0:
        return []
    residual_avg_nfv = residual_nfv_total / optional_k
    rate = 0.1
    max_rate = 0.0
    results = []
    start_time = time.time()
    while True:
        if timeout > 0 and time.time() - start_time > timeout:
            return results
        if cancel_check is not None and cancel_check():
            raise ComputationCancelled
        for sub in substrates:
            sub.apply_rate(rate)
        filtered = update_dominance(substrates, optional_k)
        filtered = sort_substrate(filtered)
        solutions = backpack(
            filtered, optional_k, residual_avg_nfv, timeout, return_topk, cancel_check=cancel_check
        )
        if solutions is None:
            return results
        best_optional_solution = solutions[-1]
        # best_solution = [*required, *best_optional_solution]
        cost = fixed_cost + sum(s.price for s in best_optional_solution)
        expectation = fixed_expectation + sum(s.expectation for s in best_optional_solution)
        if not math.isfinite(cost) or cost <= 0:
            return results
        phi = expectation - cost * rate
        rate = expectation / cost
        if rate > max_rate:
            max_rate = rate
        if phi < 1e-8:
            candidate_solutions = solutions
            if preserve_outcome_mix:
                solution_accepts = None
                if break_even_range is not None and break_even_price_map is not None:
                    lower, upper = break_even_range

                    def solution_accepts(optional_solution: list[SkinInstance]) -> bool:
                        return _solution_matches_break_even_range(
                            [*required, *optional_solution],
                            k,
                            break_even_price_map,
                            lower,
                            upper,
                        )

                remaining_timeout = timeout
                if timeout > 0:
                    remaining_timeout = max(
                        0.001,
                        timeout - (time.time() - start_time),
                    )
                diverse_substrates = _update_dominance_per_outcome_mix(
                    substrates,
                    optional_k,
                )
                diverse_substrates = sort_substrate(diverse_substrates)
                diverse_solutions = backpack_preserving_outcome_mix(
                    diverse_substrates,
                    optional_k,
                    residual_avg_nfv,
                    remaining_timeout,
                    return_topk_per_mix=min(5, max(1, return_topk)),
                    cancel_check=cancel_check,
                    solution_accepts=solution_accepts,
                )
                candidate_solutions = diverse_solutions
            for optional_solution in candidate_solutions:
                solution = sort_solution([*required, *optional_solution])
                cost = fixed_cost + sum(s.price for s in optional_solution)
                expectation = fixed_expectation + sum(s.expectation for s in optional_solution)
                if not math.isfinite(cost) or cost <= 0:
                    continue
                nfvs = [s.normalized_value for s in solution]
                avg_nfv = sum(nfvs) / len(nfvs)
                results.append({
                    "solution": solution,
                    "rate": expectation / cost - 1,
                    "cost": cost,
                    "expectation": expectation,
                    "avg_nfv": avg_nfv,
                })
            return results

def get_k_from_quality(quality: str) -> int:
    """底物品质为隐秘则 k=5，否则 k=10。"""
    return 5 if quality == "隐秘" else 10


def tradeup_average_normalized_float32(
    substrates: list[tuple[SkinTemplate, float]],
) -> float:
    """按统一的 binary32 口径计算底物平均归一化磨损。

    每件真实磨损、归一化结果、逐项累加以及最终平均都显式舍入为
    IEEE754 binary32，避免调用方因 ``numpy.float32``/Python ``float``
    类型传播不同而得到相邻的两个结果。
    """
    if not substrates:
        raise ValueError("无底物")
    total = wear_as_float32(0.0)
    for template, raw_wear in substrates:
        span = float(template.max_float) - float(template.min_float)
        if span <= 0:
            normalized = 0.0
        else:
            wear = wear_as_float32(raw_wear)
            normalized = wear_as_float32(
                (wear - float(template.min_float)) / span
            )
            normalized = max(0.0, min(1.0, normalized))
        total = wear_as_float32(total + normalized)
    return wear_as_float32(total / len(substrates))


def tradeup_product_wear_float32(
    avg_nfv: float,
    product_template: SkinTemplate,
) -> float:
    """把平均归一化磨损映射为最终可表示的 CS2 binary32 产物磨损。"""
    normalized = max(0.0, min(1.0, wear_as_float32(avg_nfv)))
    output = float(product_template.min_float) + normalized * (
        float(product_template.max_float) - float(product_template.min_float)
    )
    return wear_as_float32(output)


def compute_tradeup_simulation_products(
    substrates: list[tuple[SkinTemplate, float]],
) -> tuple[str | None, list[dict], float | None]:
    """炼金模拟：与 ``_finalize_recipes_from_rate_results`` 相同的概率与磨损模型。

    每件底物以 ``1/k`` 的概率被选中为「产出条」，再在其 ``upper_skins`` 内均匀随机；
    产物磨损为各底物归一化磨损算术平均映射到产物模板区间（并做 float32 舍入）。

    返回 ``(error_message, rows, avg_nfv)``；成功时第三项为底物归一化磨损算术平均。
    ``rows`` 按名称与概率排序，每项含
    ``name``、``float_value``、``prob``、``skin_template``、``weapon_box``（来源箱中文名，多个时 ``、`` 连接）。
    """
    if not substrates:
        return "无底物", [], None
    qualities = {t.quality for t, _ in substrates}
    if len(qualities) != 1:
        return "底物品质数据不一致", [], None
    quality = next(iter(qualities))
    k = get_k_from_quality(quality)
    if len(substrates) != k:
        return f"底物数量应为 {k} 件", [], None

    avg_nfv = tradeup_average_normalized_float32(substrates)

    pid_map = get_pid_map()
    product_probs: dict[tuple, dict] = {}

    for tpl_sub, _wear in substrates:
        uppers = tpl_sub.upper_skins or []
        n_upper = len(uppers)
        if n_upper == 0:
            continue
        prob_each = (1.0 / k) / n_upper
        for pid in uppers:
            tpl = pid_map.get(str(pid))
            if not tpl:
                continue
            name = f"{tpl.weapon_name} | {tpl.skin_name}" if tpl.skin_name else tpl.weapon_name
            float_val = tradeup_product_wear_float32(avg_nfv, tpl)
            key = (pid, float_val)
            if key not in product_probs:
                wb_list = tpl.weapon_box_name or []
                weapon_box = "、".join(wb_list) if wb_list else ""
                product_probs[key] = {
                    "name": name,
                    "float_value": float(float_val),
                    "prob": 0.0,
                    "skin_template": tpl,
                    "weapon_box": weapon_box,
                }
            product_probs[key]["prob"] += prob_each

    rows = list(product_probs.values())
    rows.sort(key=lambda x: (x["name"], x["float_value"], -x["prob"]))
    return None, rows, avg_nfv


def enrich_tradeup_simulation_rows_with_prices(
    rows: list[dict],
    avg_nfv: float,
    price_map: dict,
) -> None:
    """为模拟产物行写入 ``price``（与 ``_product_prob_map_for_recipe_ui`` 相同 lookup 规则）。"""
    for r in rows:
        tpl = r["skin_template"]
        box_id = tpl.weapon_box_id[0] if tpl.weapon_box_id else 0
        r["price"] = lookup_pid_price_at_nfv(
            price_map, box_id, tpl.quality, tpl.stat_trak, avg_nfv, str(tpl.paint_index)
        )


def substrate_prices_for_simulation(
    substrates: list[tuple[SkinTemplate, float]],
    price_map: dict,
) -> list[float]:
    """按各底物自身磨损对应的归一化磨损档位查价（与库存 ``lookup_inventory_item_price_value`` 一致）。"""
    out: list[float] = []
    for tpl, wear in substrates:
        nfv = SkinTemplate.float_to_normalized(np.float32(wear), tpl.min_float, tpl.max_float)
        box_id = tpl.weapon_box_id[0] if tpl.weapon_box_id else 0
        p = lookup_pid_price_at_nfv(
            price_map, box_id, tpl.quality, tpl.stat_trak, nfv, str(tpl.paint_index)
        )
        out.append(float(p))
    return out


def apply_simulation_prices_and_recipe_metrics(
    substrates: list[tuple[SkinTemplate, float]],
    rows: list[dict],
    price_map: dict,
    avg_nfv: float,
) -> tuple[list[float], dict[str, Any]]:
    """写入产物标价并汇总成本、期望、收益率、保本率（与配方结果字典字段一致，供 ``format_recipe_summary_line`` 使用）。"""
    enrich_tradeup_simulation_rows_with_prices(rows, avg_nfv, price_map)
    sub_prices = substrate_prices_for_simulation(substrates, price_map)
    cost = float(sum(sub_prices))
    expectation = sum(float(r["prob"]) * float(r.get("price") or 0.0) for r in rows)
    rate = (expectation / cost - 1.0) if cost > 0 else 0.0
    break_even_rate = sum(
        float(r["prob"]) for r in rows if float(r.get("price") or 0.0) > cost
    )
    recipe: dict[str, Any] = {
        "cost": cost,
        "expectation": float(expectation),
        "rate": float(rate),
        "break_even_rate": float(break_even_rate),
        "avg_nfv": float(avg_nfv),
        # 本次模拟实际使用的底物槽数（5 或 10），与五合一/十合一 UI 一致；保存配方时以此为准
        "simulation_slot_count": int(len(substrates)),
    }
    return sub_prices, recipe


def _compute_recipe_inputs(
    selected_data: list[dict],
) -> Union[str, tuple[list[SkinInstance], list[SkinInstance], list[SkinInstance], int]]:
    """解析底物；失败返回错误文案，成功返回 (全部底物, 可选底物, 必选底物, k)。"""
    if not selected_data:
        return "请先选择底物数据"
    qualities = set()
    for d in selected_data:
        tpl = get_template_from_goods_name(d.get("goods_name", ""))
        if tpl:
            qualities.add(tpl.quality)
    if len(qualities) > 1:
        return "请选择单一品质的底物"
    quality = next(iter(qualities))
    k = get_k_from_quality(quality)
    required_pairs, optional_pairs = split_required_instance_pairs(selected_data)
    required_instances = [inst for _, inst in required_pairs]
    optional_instances = [inst for _, inst in optional_pairs]
    instances = [*required_instances, *optional_instances]
    if not instances:
        return "无法解析底物数据"
    if len(instances) < k:
        return f"有效底物数量不足 {k}"
    if len(required_instances) >= k:
        return f"必选底物最多只能选择 {k - 1} 个"
    if len(optional_instances) < k - len(required_instances):
        return "非必选底物数量不足，无法补足配方"
    return (instances, optional_instances, required_instances, k)


def _substrates_display(solution: list[SkinInstance]) -> list[dict[str, Any]]:
    """与特殊磨损 ``_recipe_from_solution`` 共用，保证各模式底物表 JSON/UI 一致。"""
    return [
        {
            "name": s.name,
            "float_value": float(s.float_value),
            "price": s.price,
            "weapon_box": (
                s.skin_template.weapon_box_name[0]
                if s.skin_template.weapon_box_name
                else ""
            ),
            "platform": s.platform,
            "uuid": s.uuid,
            "purchase_link": s.purchase_link,
            "steam_assetid": s.steam_assetid,
            "steam_profile_id": s.steam_profile_id,
            "steam_id": s.steam_id,
        }
        for s in solution
    ]


def _product_prob_map_for_recipe_ui(
    solution: list[SkinInstance],
    k: int,
    avg_nfv: float,
    price_map: dict,
) -> dict[tuple, dict[str, Any]]:
    """给定底物组合与平均归一化磨损，累积产物概率、展示名、分 paint 标价（扫描/目标/特殊磨损共用）。"""
    pid_map = get_pid_map()
    product_probs: dict[tuple, dict[str, Any]] = {}
    for sub in solution:
        uppers = sub.skin_template.upper_skins or []
        n_upper = len(uppers)
        if n_upper == 0:
            continue
        prob_each = (1.0 / k) / n_upper
        for pid in uppers:
            tpl = pid_map.get(str(pid))
            if not tpl:
                continue
            name = f"{tpl.weapon_name} | {tpl.skin_name}" if tpl.skin_name else tpl.weapon_name
            float_val = tradeup_product_wear_float32(avg_nfv, tpl)
            appearance = SkinInstance.get_appearance(float_val)
            if appearance and "|" in name:
                name = f"{name}（{appearance}）"
            key = (pid, float_val)
            wb = tpl.weapon_box_name
            weapon_box = "、".join(wb) if wb else ""
            box_id = tpl.weapon_box_id[0] if tpl.weapon_box_id else 0
            pprice = lookup_pid_price_at_nfv(
                price_map, box_id, tpl.quality, tpl.stat_trak, avg_nfv, str(tpl.paint_index)
            )
            if key not in product_probs:
                product_probs[key] = {
                    "name": name,
                    "float_value": float(float_val),
                    "prob": 0.0,
                    "weapon_box": weapon_box,
                    "price": pprice,
                }
            product_probs[key]["prob"] += prob_each
    return product_probs


def _products_display_from_prob_map(
    product_probs: dict[tuple, dict[str, Any]],
) -> list[dict[str, Any]]:
    out = [
        {
            "name": v["name"],
            "float_value": v["float_value"],
            "prob": v["prob"],
            "weapon_box": v.get("weapon_box", ""),
            "price": v.get("price", 0.0),
        }
        for v in product_probs.values()
    ]
    out.sort(key=lambda x: -x["prob"])
    return out


def _rate_result_fingerprint(result: dict[str, Any]) -> tuple:
    solution = result.get("solution") or []
    return tuple(
        sorted(
            (
                getattr(s, "name", ""),
                getattr(s, "float_value", None),
                getattr(s, "platform", ""),
            )
            for s in solution
        )
    )


def _display_substrate_identity(substrate: dict[str, Any]) -> tuple[str, str, str] | None:
    """结果配方中一件底物的稳定身份键，与步骤一的皮肤/磨损/平台去重规则一致。"""
    name = canonical_goods_name_for_lookup(str(substrate.get("name") or ""))
    platform = str(substrate.get("platform") or "").strip().lower()
    if not name or not platform:
        return None
    try:
        float_value = format(
            float(np.float32(float(substrate.get("float_value")))),
            ".18f",
        )
    except (TypeError, ValueError):
        return None
    return name, float_value, platform


def _selected_row_identity(row: dict[str, Any]) -> tuple[str, str, str] | None:
    name = canonical_goods_name_for_lookup(
        str(row.get("goods_name") or row.get("name") or "")
    )
    platform = str(row.get("platform") or "").strip().lower()
    if not name or not platform:
        return None
    try:
        float_value = format(
            float(np.float32(float(row.get("float_value")))),
            ".18f",
        )
    except (TypeError, ValueError):
        return None
    return name, float_value, platform


def remove_recipe_substrates_from_rows(
    rows: list[dict],
    recipe: dict,
) -> list[dict]:
    """Remove one physical occurrence per recipe slot from the candidate pool.

    A counter is deliberately used instead of a set: separate items can share
    the same skin, float and platform and must still count as separate stock.
    """
    required = Counter(
        identity
        for substrate in recipe.get("substrates_display") or []
        if isinstance(substrate, dict)
        if (identity := _display_substrate_identity(substrate)) is not None
    )
    remaining: list[dict] = []
    for row in rows:
        identity = _selected_row_identity(row)
        if identity is not None and required[identity] > 0:
            required[identity] -= 1
            continue
        remaining.append(row)
    return remaining


def highest_expectation_recipe(recipes: list[dict]) -> dict | None:
    """Pick the recipe that owns its materials in non-overlapping mode."""
    if not recipes:
        return None
    return min(
        enumerate(recipes),
        key=lambda pair: (
            -float(pair[1].get("expectation") or 0.0),
            -float(pair[1].get("rate") or 0.0),
            float(pair[1].get("cost") or 0.0),
            pair[0],
        ),
    )[1]


def filter_non_overlapping_recipes(recipes: list[dict]) -> list[dict]:
    """按期望值从高到低保留配方，保证任何底物最多出现在一条结果中。"""
    ordered = sorted(
        enumerate(recipes),
        key=lambda pair: (
            -float(pair[1].get("expectation") or 0.0),
            -float(pair[1].get("rate") or 0.0),
            float(pair[1].get("cost") or 0.0),
            pair[0],
        ),
    )
    used_substrates: set[tuple[str, str, str]] = set()
    selected: list[dict] = []
    for _original_index, recipe in ordered:
        identities = {
            identity
            for substrate in recipe.get("substrates_display") or []
            if isinstance(substrate, dict)
            if (identity := _display_substrate_identity(substrate)) is not None
        }
        if identities & used_substrates:
            continue
        selected.append(recipe)
        used_substrates.update(identities)
    return selected


def _merge_rate_results_into_best(
    best: dict[tuple, dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    for r in results:
        fp = _rate_result_fingerprint(r)
        c = float(r.get("cost", 0))
        if fp not in best or c < float(best[fp].get("cost", 0)):
            best[fp] = r


def _break_even_rate_from_prob_map(
    product_probs: dict[tuple, dict[str, Any]],
    cost: float,
) -> float:
    return sum(
        v["prob"]
        for v in product_probs.values()
        if float(v.get("price") or 0) > cost
    )


def _expectation_rate_from_prob_map(
    product_probs: dict[tuple, dict[str, Any]],
    cost: float,
) -> tuple[float, float]:
    expectation = sum(
        float(v["prob"]) * float(v.get("price") or 0) for v in product_probs.values()
    )
    rate = (expectation / cost - 1.0) if cost > 0 else 0.0
    return expectation, rate


def _solution_matches_break_even_range(
    solution: list[SkinInstance],
    k: int,
    price_map: dict,
    min_break_even_rate: float,
    max_break_even_rate: float,
) -> bool:
    """Evaluate the exact product probabilities before a candidate leaves search."""
    if len(solution) != k or k <= 0:
        return False
    avg_nfv = sum(float(sub.normalized_value) for sub in solution) / k
    product_probs = _product_prob_map_for_recipe_ui(
        solution,
        k,
        avg_nfv,
        price_map,
    )
    break_even_rate = _break_even_rate_from_prob_map(
        product_probs,
        sum(float(sub.price) for sub in solution),
    )
    lower = max(0.0, min(1.0, float(min_break_even_rate)))
    upper = max(0.0, min(1.0, float(max_break_even_rate)))
    return lower <= break_even_rate <= upper


def _filter_recipes_by_break_even_range(
    recipes: list[dict],
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> list[dict]:
    """Return recipes whose break-even rate is inside the inclusive range."""
    lower = max(0.0, min(1.0, float(min_break_even_rate)))
    upper = max(0.0, min(1.0, float(max_break_even_rate)))
    if lower > upper:
        return []
    return [
        recipe
        for recipe in recipes
        if lower <= float(recipe.get("break_even_rate") or 0) <= upper
    ]


def _break_even_range_is_constrained(
    min_break_even_rate: float,
    max_break_even_rate: float,
) -> bool:
    lower = max(0.0, min(1.0, float(min_break_even_rate)))
    upper = max(0.0, min(1.0, float(max_break_even_rate)))
    return lower > 0.0 or upper < 1.0


def _finalize_recipes_from_rate_results(
    results: list[dict],
    k: int,
    price_map: dict,
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> list[dict]:
    """将 dinkelbach 产出的多条结果去重、按展示收益率取 top N，并组装 UI 用 recipe 字典。

    期望/收益率/保本率均从 ``_product_prob_map_for_recipe_ui`` 重算，与产物表及特殊磨损模式一致；
    Dinkelbach 仅用于筛选底物组合；最终仅保留保本率落在给定闭区间内的配方。
    """
    if not results:
        return []
    best_by_fp: dict[tuple, dict[str, Any]] = {}
    _merge_rate_results_into_best(best_by_fp, results)
    recipes = []
    for r in best_by_fp.values():
        solution = r["solution"]
        cost = r["cost"]
        avg_nfv = r["avg_nfv"]
        substrates_display = _substrates_display(solution)
        product_probs = _product_prob_map_for_recipe_ui(solution, k, avg_nfv, price_map)
        expectation, rate = _expectation_rate_from_prob_map(product_probs, cost)
        break_even_rate = _break_even_rate_from_prob_map(product_probs, cost)
        products_display = _products_display_from_prob_map(product_probs)
        recipes.append({
            "solution": solution,
            "rate": rate,
            "cost": cost,
            "expectation": expectation,
            "avg_nfv": float(avg_nfv),
            "break_even_rate": break_even_rate,
            "substrates_display": substrates_display,
            "products_display": products_display,
        })
    recipes.sort(key=lambda x: x["rate"], reverse=True)
    recipes = _filter_recipes_by_break_even_range(
        recipes,
        min_break_even_rate,
        max_break_even_rate,
    )
    return recipes[:ALCHEMY_RESULT_DISPLAY_TOP_N]


def prepare_scan_parallel_inputs(
    selected_data: list[dict],
    price_map: dict,
    norm_min: float,
    norm_max: float,
) -> Union[str, tuple[int, list[float], dict[tuple, list[float]], list[SkinInstance], list[SkinInstance], list[SkinInstance]]]:
    """供 ProcessPoolExecutor 扫描模式使用。成功返回 (k, nfv_list, sorted_nfv_cache, 全部底物, 可选底物, 必选底物)。"""
    inp = _compute_recipe_inputs(selected_data)
    if isinstance(inp, str):
        return inp
    instances, optional_instances, required_instances, k = inp
    nfv_list = find_all_nfv_in_range(instances, price_map, norm_min, norm_max)
    if not nfv_list:
        return "在设定范围内无可用归一化磨损数据"
    sorted_nfv_cache = _build_sorted_nfv_cache(instances, price_map)
    return (k, nfv_list, sorted_nfv_cache, instances, optional_instances, required_instances)


def worker_scan_single_nfv(
    instances: list[SkinInstance],
    optional_instances: list[SkinInstance],
    required_instances: list[SkinInstance],
    price_map: dict,
    nfv: float,
    k: int,
    sorted_nfv_cache: dict[tuple, list[float]],
    timeout: float,
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> list[dict]:
    """子进程入口：单个归一化磨损点的 dinkelbach 扫描。须为模块级函数以便 pickle。
    instances 由 prepare 一次后随任务 pickle 传入；各子进程为独立反序列化副本，可原地 assign_expectation。"""
    return _worker_scan_single_nfv_impl(
        instances,
        optional_instances,
        required_instances,
        price_map,
        nfv,
        k,
        sorted_nfv_cache,
        timeout,
        min_break_even_rate,
        max_break_even_rate,
    )


_scan_pool_ctx: dict = {}


def init_scan_worker_pool(
    instances: list[SkinInstance],
    optional_instances: list[SkinInstance],
    required_instances: list[SkinInstance],
    price_map: dict,
    sorted_nfv_cache: dict[tuple, list[float]],
    k: int,
    timeout: float,
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> None:
    """ProcessPool 各 worker 仅初始化一次，避免每个 nfv 任务重复 pickle 大体量底物数据。"""
    global _scan_pool_ctx
    _scan_pool_ctx = {
        "instances": instances,
        "optional_instances": optional_instances,
        "required_instances": required_instances,
        "price_map": price_map,
        "sorted_nfv_cache": sorted_nfv_cache,
        "k": k,
        "timeout": timeout,
        "min_break_even_rate": min_break_even_rate,
        "max_break_even_rate": max_break_even_rate,
    }


def worker_scan_single_nfv_task(nfv: float) -> list[dict]:
    """扫描池 worker 任务：仅传 nfv，其余数据来自 ``init_scan_worker_pool``。"""
    ctx = _scan_pool_ctx
    if not ctx:
        return []
    return _worker_scan_single_nfv_impl(
        ctx["instances"],
        ctx["optional_instances"],
        ctx["required_instances"],
        ctx["price_map"],
        nfv,
        ctx["k"],
        ctx["sorted_nfv_cache"],
        ctx["timeout"],
        ctx["min_break_even_rate"],
        ctx["max_break_even_rate"],
    )


def _worker_scan_single_nfv_impl(
    instances: list[SkinInstance],
    optional_instances: list[SkinInstance],
    required_instances: list[SkinInstance],
    price_map: dict,
    nfv: float,
    k: int,
    sorted_nfv_cache: dict[tuple, list[float]],
    timeout: float,
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> list[dict]:
    if not instances:
        return []
    assign_expectation(instances, nfv, k, price_map, sorted_nfv_cache)
    res = dinkelbach(
        optional_instances,
        k,
        nfv,
        timeout,
        return_topk=ALCHEMY_SCAN_MODE_DINKELBACH_TOPK,
        required_substrates=required_instances,
        preserve_outcome_mix=_break_even_range_is_constrained(
            min_break_even_rate,
            max_break_even_rate,
        ),
        break_even_range=(min_break_even_rate, max_break_even_rate),
        break_even_price_map=price_map,
    )
    return res if res else []


def compute_recipes(
    selected_data: list[dict],
    price_map: dict,
    norm_min: float,
    norm_max: float,
    mode: str = "scan",
    timeout: float = 30,
    progress_queue: Optional[multiprocessing.Queue] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    min_break_even_rate: float = 0.0,
    max_break_even_rate: float = 1.0,
) -> tuple[list[dict], Optional[str]]:
    """
    计算 top10 配方。
    返回 (recipes, error_msg)。recipes 每项为 {
        solution, rate, cost, expectation, avg_nfv, break_even_rate,
        substrates_display, products_display
    }
    """
    inp = _compute_recipe_inputs(selected_data)
    if isinstance(inp, str):
        return [], inp
    instances, optional_instances, required_instances, k = inp
    results = []
    if mode == "scan":
        nfv_list = find_all_nfv_in_range(instances, price_map, norm_min, norm_max)
        if not nfv_list:
            return [], "在设定范围内无可用归一化磨损数据"
        sorted_nfv_cache = _build_sorted_nfv_cache(instances, price_map)
        total = len(nfv_list)
        for i, nfv in enumerate(nfv_list):
            if cancel_check is not None and cancel_check():
                raise ComputationCancelled
            assign_expectation(instances, nfv, k, price_map, sorted_nfv_cache)
            res = dinkelbach(
                optional_instances,
                k,
                nfv,
                timeout,
                return_topk=ALCHEMY_SCAN_MODE_DINKELBACH_TOPK,
                cancel_check=cancel_check,
                required_substrates=required_instances,
                preserve_outcome_mix=_break_even_range_is_constrained(
                    min_break_even_rate,
                    max_break_even_rate,
                ),
                break_even_range=(min_break_even_rate, max_break_even_rate),
                break_even_price_map=price_map,
            )
            results.extend(res)
            if progress_queue is not None:
                try:
                    progress_queue.put_nowait(int((i + 1) / total * 100))
                except Exception:
                    pass
    elif mode == "target":
        target_nfv = norm_min  # 目标模式 norm_min == norm_max
        assign_expectation(instances, target_nfv, k, price_map)
        res = dinkelbach(
            optional_instances,
            k,
            target_nfv,
            timeout,
            return_topk=ALCHEMY_TARGET_MODE_DINKELBACH_TOPK,
            cancel_check=cancel_check,
            required_substrates=required_instances,
            preserve_outcome_mix=_break_even_range_is_constrained(
                min_break_even_rate,
                max_break_even_rate,
            ),
            break_even_range=(min_break_even_rate, max_break_even_rate),
            break_even_price_map=price_map,
        )
        if cancel_check is not None and cancel_check():
            raise ComputationCancelled
        results.extend(res)
    else:
        raise ValueError(f"无效的 mode: {mode}")

    recipes = _finalize_recipes_from_rate_results(
        results,
        k,
        price_map,
        min_break_even_rate=min_break_even_rate,
        max_break_even_rate=max_break_even_rate,
    )
    return recipes, None
