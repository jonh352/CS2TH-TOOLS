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
