"""Steam 库存 HTTP API（单 context）。"""

from __future__ import annotations

import json
from typing import Any

import requests

from .constants import BROWSER_HEADERS, STEAM_APP_ID
from .errors import SteamSessionExpiredError
from .proxy import resolve_system_http_proxy_for_steam


def request_steam_inventory_context(
    cookie: str,
    context_id: int,
    *,
    steam_id: str,
    app_id: int = STEAM_APP_ID,
) -> dict[str, Any] | None:
    """请求单页库存 JSON；会话失效时抛 SteamSessionExpiredError。"""
    url = (
        f"https://steamcommunity.com/inventory/{steam_id}/{app_id}/{context_id}"
        "?l=schinese&count=2000&raw_asset_properties=1"
    )
    headers = {
        **BROWSER_HEADERS,
        "Accept": "application/json",
        "Origin": "https://steamcommunity.com",
        "Cookie": cookie,
    }
    proxy = resolve_system_http_proxy_for_steam()
    kwargs: dict = {"url": url, "headers": headers, "timeout": 30}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    resp = requests.get(**kwargs)

    if resp.status_code in (401, 403):
        raise SteamSessionExpiredError()
    resp.raise_for_status()

    raw = resp.text or ""
    try:
        data = resp.json()
    except json.JSONDecodeError:
        head = raw[:4000].lower()
        if "<html" in head or "login" in head or "sign in" in head:
            raise SteamSessionExpiredError()
        raise RuntimeError("Steam 库存响应无效，本次不更新库存或判断离库")

    if isinstance(data, dict):
        success = data.get("success")
        if success is False or success == 0:
            raise SteamSessionExpiredError()

    if not isinstance(data, dict) or data.get("success") not in (True, 1):
        raise RuntimeError("Steam 库存未完整返回，本次不更新库存或判断离库")
    assets = data.get("assets") or []
    expected = data.get("total_inventory_count")
    if data.get("more_items") or (expected is not None and len(assets) < int(expected)):
        raise RuntimeError("Steam 库存仅返回部分物品，本次不更新库存或判断离库，请稍后重试")
    return data
