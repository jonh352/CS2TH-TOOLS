"""Parse CS2TH recipe links and load material data for the marketplace page."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import requests

from config import APP_VERSION, AUTH_API_BASE_URL

_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_FLOAT_RE = re.compile(r"(?<![\d.])(?:0(?:\.\d+)?|1(?:\.0+)?)(?![\d.])")


def parse_recipe_reference(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if not text:
        raise ValueError("请输入 CS2TH 配方链接")
    if _RECIPE_ID_RE.fullmatch(text):
        return text, "spot"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "cs2th.cn",
        "www.cs2th.cn",
    }:
        raise ValueError("仅支持 cs2th.cn 的配方链接")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "recipe" or not _RECIPE_ID_RE.fullmatch(parts[1]):
        raise ValueError("配方链接格式不正确")
    market = (parse_qs(parsed.query).get("market") or ["spot"])[0].lower()
    if market not in {"spot", "futures"}:
        raise ValueError("配方市场必须是 spot 或 futures")
    return parts[1], market


def fetch_recipe_detail(reference: str, access_token: str = "") -> dict:
    recipe_id, market = parse_recipe_reference(reference)
    headers = {
        "X-CS2TH-Client": "cs2th-tools",
        "X-CS2TH-Version": APP_VERSION,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = requests.get(
            f"{AUTH_API_BASE_URL}/api/recipes/{recipe_id}",
            params={"market": market},
            headers=headers,
            timeout=20,
        )
        payload = response.json() if response.text else {}
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"获取配方失败：{exc}") from exc
    if not response.ok:
        detail = payload.get("detail") if isinstance(payload, dict) else ""
        raise RuntimeError(str(detail or f"获取配方失败（HTTP {response.status_code}）"))
    if not isinstance(payload, dict) or not isinstance(payload.get("inputs"), list):
        raise RuntimeError("CS2TH 返回的配方材料格式无效")
    payload["_recipe_id"] = recipe_id
    payload["_market"] = market
    return payload


def material_wear_range(material: dict) -> tuple[float | None, float | None, str]:
    raw_label = str(material.get("float_range") or material.get("float_bucket") or "").strip()
    values = [float(value) for value in _FLOAT_RE.findall(raw_label)]
    if len(values) >= 2:
        low, high = values[0], values[1]
        return low, high, raw_label
    unit_float = float(material.get("unit_float") or 0)
    if unit_float > 0:
        return None, unit_float, f"目标磨损 ≤ {unit_float:.8f}"
    wear = str(material.get("wear") or "未注明")
    min_float = float(material.get("min_float") or 0)
    max_float = float(material.get("max_float") or 1)
    return None, None, f"{wear} · 饰品范围 {min_float:.4f}–{max_float:.4f}"


def _exclude_skin_base_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"^纪念品\s+", "", text)
    text = re.sub(r"^Souvenir\s+", "", text, flags=re.I)
    text = re.sub(r"（纪念品）$", "", text)
    text = re.sub(r"\s*\(Souvenir\)$", "", text, flags=re.I)
    return text.strip()


def fetch_material_alternatives(
    *,
    collection_name: str,
    rarity: str,
    wear: str,
    exclude_name: str,
    market: str = "spot",
    normalized: float | None = None,
    access_token: str = "",
) -> list[dict]:
    """Load same-collection / same-rarity sibling skins for one recipe input."""
    collection_name = str(collection_name or "").strip()
    rarity = str(rarity or "").strip()
    wear = str(wear or "").strip()
    if not collection_name or not rarity or not wear:
        return []
    headers = {
        "X-CS2TH-Client": "cs2th-tools",
        "X-CS2TH-Version": APP_VERSION,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    params: dict[str, str] = {
        "collection_name": collection_name,
        "rarity": rarity,
        "wear": wear,
        "exclude": _exclude_skin_base_name(exclude_name),
        "market": market if market in {"spot", "futures"} else "spot",
    }
    if normalized is not None:
        try:
            params["normalized"] = str(float(normalized))
        except (TypeError, ValueError):
            pass
    try:
        response = requests.get(
            f"{AUTH_API_BASE_URL}/api/collections/rarity-skins",
            params=params,
            headers=headers,
            timeout=20,
        )
        payload = response.json() if response.text else {}
    except (requests.RequestException, ValueError):
        return []
    if not response.ok or not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def alternative_to_recipe_material(alt: dict, primary: dict) -> dict:
    """Map a rarity-skins item onto the recipe-material card schema."""
    wear = str(primary.get("wear") or "").strip()
    equiv = alt.get("equiv_float")
    try:
        unit_float = float(equiv) if equiv is not None else float(primary.get("unit_float") or 0)
    except (TypeError, ValueError):
        unit_float = float(primary.get("unit_float") or 0)
    float_range = str(alt.get("float_range") or "").strip()
    material = {
        "name": str(alt.get("name") or alt.get("market_hash_name") or "未知备选"),
        "market_hash_name": str(alt.get("market_hash_name") or ""),
        "market_hash_name_en": str(alt.get("market_hash_name_en") or ""),
        "count": 0,
        "wear": wear,
        "float_range": float_range,
        "unit_float": unit_float,
        "unit_normalized": alt.get("equiv_normalized", primary.get("unit_normalized")),
        "unit_price_cny": float(alt.get("unit_price_cny") or 0),
        "collection_name": str(primary.get("collection_name") or ""),
        "goods_id": alt.get("goods_id"),
        "c5_id": alt.get("c5_id"),
        "youpin_id": alt.get("youpin_id"),
        "eco_id": alt.get("eco_id"),
        "min_float": alt.get("min_float"),
        "max_float": alt.get("max_float"),
        "is_alternative": True,
        "supports_wear": bool(alt.get("supports_wear", True)),
    }
    return material


def attach_recipe_alternatives(payload: dict, access_token: str = "") -> dict:
    """Fetch and attach alternatives for each primary input onto the recipe payload."""
    inputs = [item for item in payload.get("inputs", []) if isinstance(item, dict)]
    default_rarity = str(payload.get("input_rarity") or "").strip()
    market = str(payload.get("_market") or "spot")
    alternatives: dict[int, list[dict]] = {}
    for index, item in enumerate(inputs):
        rarity = str(
            item.get("quality") or item.get("rarity") or default_rarity or ""
        ).strip()
        try:
            normalized = item.get("unit_normalized")
            normalized_value = (
                float(normalized) if normalized not in (None, "") else None
            )
        except (TypeError, ValueError):
            normalized_value = None
        siblings = fetch_material_alternatives(
            collection_name=str(item.get("collection_name") or ""),
            rarity=rarity,
            wear=str(item.get("wear") or ""),
            exclude_name=str(item.get("name") or ""),
            market=market,
            normalized=normalized_value,
            access_token=access_token,
        )
        alternatives[index] = [
            alternative_to_recipe_material(sibling, item) for sibling in siblings
        ]
    payload["_alternatives_by_input"] = alternatives
    return payload


def saved_recipe_to_bridge_payload(recipe: dict, *, title: str = "") -> dict:
    """Build a CS2TH-style bridge payload from a local saved recipe.

    Enables the same material-card / alternatives UI used by link-loaded recipes.
    """
    from core.alchemy_quality import (
        get_name_map,
        normalize_name,
        strip_appearance_suffix_from_goods_name,
    )
    from core.data_utils import SkinInstance, get_interval_value

    substrates = recipe.get("substrates_display")
    if not isinstance(substrates, list):
        substrates = []
    grouped: dict[tuple[str, float, float], dict] = {}
    for substrate in substrates:
        if not isinstance(substrate, dict):
            continue
        name = strip_appearance_suffix_from_goods_name(
            str(substrate.get("name") or "").strip()
        )
        if not name:
            continue
        try:
            wear_value = float(substrate.get("float_value"))
        except (TypeError, ValueError):
            continue
        template = get_name_map().get(normalize_name(name))
        min_float = float(template.min_float) if template is not None else 0.0
        max_float = float(template.max_float) if template is not None else 1.0
        low, high = get_interval_value(wear_value)
        low = max(min_float, float(low))
        high = min(max_float, float(high))
        key = (name, low, high)
        if key not in grouped:
            box = str(substrate.get("weapon_box") or "").strip()
            if not box and template is not None and template.weapon_box_name:
                box = str(template.weapon_box_name[0] or "").strip()
            quality = str(template.quality) if template is not None else ""
            span = max_float - min_float
            try:
                normalized = (
                    (wear_value - min_float) / span if span > 1e-12 else None
                )
            except (TypeError, ValueError):
                normalized = None
            appearance = SkinInstance.get_appearance(wear_value) or ""
            grouped[key] = {
                "name": name,
                "count": 0,
                "wear": appearance,
                "wear_value": wear_value,
                "unit_float": wear_value,
                "min_wear": low,
                "max_wear": high,
                "float_range": f"{low:g} ~ {high:g}",
                "collection_name": box,
                "quality": quality,
                "unit_normalized": normalized,
                "unit_price_cny": float(substrate.get("price") or 0),
            }
        grouped[key]["count"] += 1
        price = float(substrate.get("price") or 0)
        if price > 0:
            grouped[key]["unit_price_cny"] = price

    inputs = list(grouped.values())
    rarities = [
        str(item.get("quality") or "").strip()
        for item in inputs
        if str(item.get("quality") or "").strip()
    ]
    input_rarity = rarities[0] if rarities else ""
    boxes = [
        str(item.get("collection_name") or "").strip()
        for item in inputs
        if str(item.get("collection_name") or "").strip()
    ]
    collection_label = title.strip() or (
        " × ".join(dict.fromkeys(boxes)) if boxes else "保存配方"
    )
    return {
        "inputs": inputs,
        "input_rarity": input_rarity,
        "collection_name": collection_label,
        "input_cost": float(recipe.get("cost") or 0),
        "roi": float(recipe.get("rate") or 0),
        "_market": "spot",
        "_recipe_id": str(
            recipe.get("cs2th_recipe_id") or recipe.get("id") or ""
        ),
        "outcomes": [],
    }


def cs2th_detail_to_saved_recipe(payload: dict) -> dict:
    """Convert the public CS2TH detail response to the local recipe schema."""
    inputs = [item for item in payload.get("inputs", []) if isinstance(item, dict)]
    outcomes = [
        item for item in payload.get("outcomes", []) if isinstance(item, dict)
    ]
    substrates: list[dict] = []
    for item in inputs:
        count = max(0, int(item.get("count") or 0))
        unit_float = float(item.get("unit_float") or 0)
        if unit_float <= 0:
            low, high, _label = material_wear_range(item)
            if low is not None and high is not None:
                unit_float = (low + high) / 2
        entry = {
            "name": str(item.get("name") or item.get("market_hash_name") or ""),
            "float_value": unit_float,
            "price": float(item.get("unit_price_cny") or 0),
            "weapon_box": str(item.get("collection_name") or ""),
            "platform": "cs2th",
            "purchase_link": "",
        }
        substrates.extend(dict(entry) for _ in range(count))

    products = [
        {
            "name": str(item.get("name") or item.get("market_hash_name") or ""),
            "float_value": float(item.get("output_float") or 0),
            "prob": float(item.get("probability") or 0),
            "weapon_box": str(item.get("collection_name") or ""),
            "price": float(item.get("unit_price_cny") or 0),
        }
        for item in outcomes
    ]
    cost = float(payload.get("input_cost") or 0)
    expectation = float(payload.get("expected_output") or 0)
    rate = (expectation - cost) / cost if cost else 0.0
    return {
        "cost": cost,
        "expectation": expectation,
        "rate": rate,
        "break_even_rate": float(payload.get("profit_probability") or 0),
        "avg_nfv": float(payload.get("avg_input_normalized") or 0),
        "substrates_display": substrates,
        "products_display": products,
        "simulation_slot_count": len(substrates),
        "cs2th_recipe_id": str(payload.get("_recipe_id") or payload.get("id") or ""),
        "cs2th_market": str(payload.get("_market") or "spot"),
    }
