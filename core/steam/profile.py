"""Steam 个人资料页解析与头像缓存。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from config import STEAM_AVATAR_CACHE_DIR

from .constants import BROWSER_HEADERS
from .models import SteamWebProfile
from .proxy import resolve_system_http_proxy_for_steam

if TYPE_CHECKING:
    from playwright.sync_api import Page


def normalize_steam_avatar_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/") and not u.startswith("//"):
        u = "https://steamcommunity.com" + u
    if re.fullmatch(r"[0-9a-fA-F]{40}", u):
        return f"https://avatars.steamstatic.com/{u}_full.jpg"
    return u


def _avatar_src_from_dom(page: Page) -> str:
    selectors = (
        ".playerAvatarAutoSizeInner img",
        ".playerAvatar img",
        ".user_avatar img",
        "a.user_avatar img",
    )
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            src = (
                el.get_attribute("src")
                or el.get_attribute("data-src")
                or el.get_attribute("data-srcset")
                or ""
            ).strip()
            if src:
                if " " in src and "," in src:
                    src = src.split(",")[0].strip().split()[0]
                if "steam" in src.lower() or src.startswith("//") or src.startswith("http"):
                    return src
        except Exception:
            continue
    return ""


def extract_steam_profile_from_page(page: Page) -> SteamWebProfile | None:
    try:
        data = page.evaluate(
            """() => {
            const out = { steamid: null, personaname: '', avatar: '' };
            if (typeof g_rgProfileData !== 'undefined' && g_rgProfileData) {
                const g = g_rgProfileData;
                if (g.steamid) out.steamid = String(g.steamid);
                if (g.personaname) out.personaname = String(g.personaname);
                const keys = ['avatarfull', 'avatarmedium', 'avatar'];
                for (const k of keys) {
                    const v = g[k];
                    if (!v) continue;
                    const s = String(v).trim();
                    if (!s) continue;
                    if (s.startsWith('http') || s.startsWith('//')) { out.avatar = s; break; }
                    if (/^[0-9a-f]{40}$/i.test(s)) { out.avatar = s; break; }
                }
            }
            if (!out.avatar) {
                const img = document.querySelector('.playerAvatarAutoSizeInner img')
                    || document.querySelector('.playerAvatar img');
                if (img) {
                    const s = (img.getAttribute('src') || img.getAttribute('data-src') || img.src || '').trim();
                    if (s) out.avatar = s;
                }
            }
            if (!out.steamid) {
                const el = document.querySelector('[data-miniprofile]');
                if (el) {
                    const v = el.getAttribute('data-miniprofile');
                    if (v && /^\\d{17}$/.test(v)) out.steamid = v;
                }
            }
            const m = window.location.href.match(/\\/profiles\\/(\\d{17})\\b/);
            if (m && !out.steamid) out.steamid = m[1];
            return out;
        }"""
        )
    except Exception:
        return None
    if not data or not data.get("steamid"):
        return None
    sid = str(data["steamid"])
    if not re.fullmatch(r"\d{17}", sid):
        return None
    name = (data.get("personaname") or "").strip() or "Steam 用户"
    avatar_raw = (data.get("avatar") or "").strip()
    avatar = normalize_steam_avatar_url(avatar_raw)
    if not avatar:
        avatar = normalize_steam_avatar_url(_avatar_src_from_dom(page))
    return SteamWebProfile(steam_id=sid, personaname=name, avatar_url=avatar)


def _avatar_cache_suffix(content: bytes) -> str:
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ""


def cache_steam_avatar(avatar_url: str, steam_id: str) -> str:
    """下载头像到缓存目录，返回本地绝对路径；失败返回空字符串。"""
    url = normalize_steam_avatar_url(avatar_url)
    if not url:
        return ""
    try:
        proxy = resolve_system_http_proxy_for_steam()
        kwargs: dict = {"url": url, "headers": BROWSER_HEADERS, "timeout": 25}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        r = requests.get(**kwargs)
        r.raise_for_status()
        body = r.content
        if len(body) < 32:
            return ""
        ext = _avatar_cache_suffix(body)
        fname = (
            f"steam_inventory_{steam_id}_avatar{ext}"
            if ext
            else f"steam_inventory_{steam_id}_avatar"
        )
        STEAM_AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dest = STEAM_AVATAR_CACHE_DIR / fname
        dest.write_bytes(body)
        return str(dest.resolve())
    except Exception:
        return ""
