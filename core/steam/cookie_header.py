"""从 Playwright 上下文导出与库存 API 一致的 Cookie 头。"""

from __future__ import annotations

import re
from typing import Any


def _steam_cookie_domains_ok(domain: str) -> bool:
    d = (domain or "").lower()
    return any(x in d for x in ("steamcommunity", "steampowered", "steamchina"))


def _cookie_header_from_playwright_list(cookies: list[dict[str, Any]]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for c in cookies:
        n = c.get("name")
        if not n or n in seen:
            continue
        seen.add(n)
        parts.append(f"{n}={c['value']}")
    return "; ".join(parts)


def build_steam_inventory_cookie_header(context, steam_id: str) -> str:
    """
    按库存接口 URL 从 Playwright 取 Cookie，与真实浏览器发往
    steamcommunity.com/inventory/... 一致。
    """
    sid = (steam_id or "").strip()
    urls: list[str] = [
        "https://steamcommunity.com/",
        "https://store.steampowered.com/",
    ]
    if re.fullmatch(r"\d{17}", sid):
        urls = [
            f"https://steamcommunity.com/inventory/{sid}/730/2",
            f"https://steamcommunity.com/inventory/{sid}/730/16",
            f"https://steamcommunity.com/profiles/{sid}/",
        ] + urls

    merged: list[dict] = []
    seen_name: set[str] = set()
    for u in urls:
        try:
            batch = context.cookies([u])
        except Exception:
            batch = []
        for c in batch:
            dom = c.get("domain") or ""
            if not _steam_cookie_domains_ok(dom):
                continue
            n = c.get("name") or ""
            if not n or n in seen_name:
                continue
            seen_name.add(n)
            merged.append(c)

    if not merged:
        try:
            merged = [
                c
                for c in context.cookies()
                if _steam_cookie_domains_ok(c.get("domain") or "")
            ]
        except Exception:
            merged = []

    return _cookie_header_from_playwright_list(merged)
