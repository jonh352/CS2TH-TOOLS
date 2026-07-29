"""Marketplace link generation shared by the platform and inventory pages."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from core.data_utils import APPEARANCE, APPEARANCE_MAP, SkinTemplate


@dataclass(frozen=True, slots=True)
class Marketplace:
    key: str
    name: str
    home_url: str
    logo_name: str


MARKETPLACES = (
    Marketplace("buff", "BUFF", "https://buff.163.com/market/csgo", "buff_logo.png"),
    Marketplace("yyyp", "悠悠有品", "https://www.youpin898.com/market", "yyyp_logo.png"),
    Marketplace("c5", "C5GAME", "https://www.c5game.com/csgo", "c5game_logo.png"),
    Marketplace("eco", "ECOSteam", "https://www.ecosteam.cn/market/730-1.html?game=730", "eco_logo.png"),
    Marketplace("steam", "Steam", "https://steamcommunity.com/market/", "steam_logo.jpg"),
)


def _appearance_cn(wear: str) -> str:
    return APPEARANCE_MAP.get(wear, wear) if wear else ""


def _platform_id(mapping: dict, appearance: str) -> str:
    value = mapping.get(appearance)
    if value is None and mapping:
        value = next(iter(mapping.values()), None)
    return str(value or "").strip()


def _steam_market_name(template: SkinTemplate, appearance: str) -> str:
    value = template.steam.get(appearance)
    if value:
        return str(value)
    if template.steam:
        return str(next(iter(template.steam.values())))
    base = (
        f"{template.weapon_name} | {template.skin_name}"
        if template.skin_name
        else template.weapon_name
    )
    return base


def links_for_template(
    template: SkinTemplate,
    wear: str,
    *,
    min_wear: float | None = None,
    max_wear: float | None = None,
) -> dict[str, str]:
    appearance = _appearance_cn(wear)
    result = {market.key: market.home_url for market in MARKETPLACES}

    buff_id = _platform_id(template.buff, appearance)
    if buff_id:
        fragment = "tab=selling"
        if min_wear is not None:
            fragment += f"&min_paintwear={min_wear:.8f}"
        if max_wear is not None:
            fragment += f"&max_paintwear={max_wear:.8f}"
        result["buff"] = (
            f"https://buff.163.com/goods/{quote(buff_id)}?from=market#{fragment}"
        )

    yyyp_id = _platform_id(template.yyyp, appearance)
    if yyyp_id:
        result["yyyp"] = (
            "https://www.youpin898.com/market/goods-list"
            f"?listType=10&templateId={quote(yyyp_id)}&gameId=730"
        )

    c5_id = _platform_id(template.c5, appearance)
    if c5_id:
        params = []
        if min_wear is not None:
            params.append(f"minWear={min_wear:.8f}")
        if max_wear is not None:
            params.append(f"maxWear={max_wear:.8f}")
        suffix = f"?{'&'.join(params)}" if params else ""
        result["c5"] = f"https://www.c5game.com/csgo/{quote(c5_id)}/item/sell{suffix}"

    eco_id = _platform_id(template.eco, appearance)
    if eco_id:
        result["eco"] = f"https://www.ecosteam.cn/goods/730-{quote(eco_id)}-1-laypagesale-0-1"

    steam_name = _steam_market_name(template, appearance)
    if steam_name:
        result["steam"] = (
            "https://steamcommunity.com/market/listings/730/" + quote(steam_name, safe="")
        )
    return result


def links_for_recipe_material(
    material: dict,
    *,
    min_wear: float | None = None,
    max_wear: float | None = None,
) -> dict[str, str]:
    """Build links from the live IDs returned by the CS2TH recipe API."""
    result = {market.key: market.home_url for market in MARKETPLACES}
    goods_id = str(material.get("goods_id") or "").strip()
    if goods_id:
        fragment = "tab=selling"
        if min_wear is not None:
            fragment += f"&min_paintwear={min_wear:.8f}"
        if max_wear is not None:
            fragment += f"&max_paintwear={max_wear:.8f}"
        result["buff"] = (
            f"https://buff.163.com/goods/{quote(goods_id)}?from=market#{fragment}"
        )
    youpin_id = str(material.get("youpin_id") or "").strip()
    if youpin_id:
        result["yyyp"] = (
            "https://www.youpin898.com/market/goods-list"
            f"?listType=10&templateId={quote(youpin_id)}&gameId=730"
        )
    c5_id = str(material.get("c5_id") or "").strip()
    if c5_id:
        params = []
        if min_wear is not None:
            params.append(f"minWear={min_wear:.8f}")
        if max_wear is not None:
            params.append(f"maxWear={max_wear:.8f}")
        suffix = f"?{'&'.join(params)}" if params else ""
        result["c5"] = f"https://www.c5game.com/csgo/{quote(c5_id)}/item/sell{suffix}"
    eco_id = str(material.get("eco_id") or "").strip()
    if eco_id:
        result["eco"] = (
            f"https://www.ecosteam.cn/goods/730-{quote(eco_id)}-1-laypagesale-0-1"
        )
    steam_name = str(
        material.get("market_hash_name_en") or material.get("market_hash_name") or ""
    ).strip()
    if steam_name:
        result["steam"] = (
            "https://steamcommunity.com/market/listings/730/"
            + quote(steam_name, safe="")
        )
    return result


def marketplace_by_key(key: str) -> Marketplace | None:
    return next((market for market in MARKETPLACES if market.key == key), None)
