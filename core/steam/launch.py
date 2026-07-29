"""Playwright 持久化上下文：目录解析、启动参数、stealth 注入。"""

from __future__ import annotations

import os
from pathlib import Path

from config import (
    PLAYWRIGHT_CHROME_USE_SYSTEM_USER_DATA,
    PLAYWRIGHT_CHROME_USER_DATA_DIR,
    PLAYWRIGHT_EDGE_USER_DATA_DIR,
    PLAYWRIGHT_MSEDGE_USE_SYSTEM_USER_DATA,
    PLAYWRIGHT_USER_AGENT,
)
from core.playwright_channel_prefs import load_playwright_channel_try_order
from .errors import STEAM_BROWSER_NOT_INSTALLED_MSG, SteamBrowserNotFoundError
from .proxy import resolve_system_http_proxy_for_steam
from .stealth import playwright_stealth_init_js
from .window_metrics import get_runtime_window_metrics


def resolve_playwright_user_data_dir(session_path: Path, channel: str) -> Path:
    """channel 对应环境变量 > config 显式路径 > 系统 User Data > session_path。"""
    sp = Path(session_path)

    def pick(candidates: list[Path | None]) -> Path:
        for c in candidates:
            if c is None:
                continue
            p = Path(c).expanduser().resolve()
            if p.is_dir():
                return p
        return sp

    if channel == "msedge":
        env = (os.environ.get("CS2TH_TOOLS_PLAYWRIGHT_EDGE_USER_DATA") or "").strip()
        env_p = Path(env).expanduser().resolve() if env else None
        system = None
        if PLAYWRIGHT_MSEDGE_USE_SYSTEM_USER_DATA and os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "").strip()
            if local:
                system = Path(local) / "Microsoft" / "Edge" / "User Data"
        return pick([env_p, PLAYWRIGHT_EDGE_USER_DATA_DIR, system, sp])

    if channel == "chrome":
        env = (os.environ.get("CS2TH_TOOLS_PLAYWRIGHT_CHROME_USER_DATA") or "").strip()
        env_p = Path(env).expanduser().resolve() if env else None
        system = None
        if PLAYWRIGHT_CHROME_USE_SYSTEM_USER_DATA and os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "").strip()
            if local:
                system = Path(local) / "Google" / "Chrome" / "User Data"
        return pick([env_p, PLAYWRIGHT_CHROME_USER_DATA_DIR, system, sp])

    return sp


def _resolved_playwright_user_agent() -> str | None:
    env = (os.environ.get("CS2TH_TOOLS_PLAYWRIGHT_USER_AGENT") or "").strip()
    if env:
        return env
    ua = (PLAYWRIGHT_USER_AGENT or "").strip()
    return ua or None


def persistent_launch_options(
    session_path: Path,
    *,
    headless: bool,
    viewport: tuple[int, int] | None = None,
) -> dict:
    """viewport 默认见 config PLAYWRIGHT_SCREEN_*；可传入 tuple 覆盖。

    注意：``chromium.launch_persistent_context`` **不支持** ``storage_state`` 参数；若需导入 ``steam_storage_state.json``，
    应在启动后对 ``BrowserContext`` 调用 ``add_cookies``。
    """
    metrics = get_runtime_window_metrics()
    vw, vh = (
        viewport
        if viewport is not None
        else (int(metrics["screen_width"]), int(metrics["screen_height"]))
    )
    proxy = resolve_system_http_proxy_for_steam()
    proxy_dict = {"server": proxy} if proxy else None
    ignore_default: list[str] = ["--enable-automation", "--no-sandbox"]
    opts: dict = {
        "user_data_dir": str(session_path),
        "headless": headless,
        "proxy": proxy_dict,
        "args": [
            '--disable-blink-features=AutomationControlled', 
            '--disable-features=PasswordImport',
            '--window-position=0,0',
        ],
        "ignore_default_args": ignore_default,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "extra_http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "viewport": {"width": vw, "height": vh},
    }
    ua = _resolved_playwright_user_agent()
    if ua:
        opts["user_agent"] = ua
    return opts


def attach_stealth_to_context(context) -> None:
    js = playwright_stealth_init_js()
    if not js:
        return
    try:
        context.add_init_script(js)
    except Exception:
        pass


def launch_persistent_chromium_context(
    playwright,
    session_path: Path,
    *,
    headless: bool,
    webdriver_stealth: bool = True,
    viewport: tuple[int, int] | None = None,
    channel_try_order: tuple[str, ...] | None = None,
):
    """按 ``channel_try_order`` 依次尝试 Playwright 渠道；未指定时从设置「首选浏览器」读取，再试另一种。"""
    order = (
        channel_try_order
        if channel_try_order is not None
        else load_playwright_channel_try_order()
    )
    last_exc: Exception | None = None
    for channel in order:
        try:
            data_dir = resolve_playwright_user_data_dir(session_path, channel)
            opts = persistent_launch_options(data_dir, headless=headless, viewport=viewport)
            ctx = playwright.chromium.launch_persistent_context(channel=channel, **opts)
            if webdriver_stealth:
                attach_stealth_to_context(ctx)
            return ctx
        except Exception as e:
            last_exc = e
            continue
    raise SteamBrowserNotFoundError(STEAM_BROWSER_NOT_INSTALLED_MSG) from last_exc


def launch_ephemeral_chromium_context(
    playwright,
    *,
    headless: bool,
    storage_state: str | Path | None = None,
    viewport: tuple[int, int] | None = None,
    channel_try_order: tuple[str, ...] | None = None,
):
    """
    非持久化 Browser + Context（可选 ``storage_state``）。
    返回 ``(browser, context)``：须先 ``context.close()`` 再 ``browser.close()``。
    """
    order = (
        channel_try_order
        if channel_try_order is not None
        else load_playwright_channel_try_order()
    )
    last_exc: Exception | None = None
    metrics = get_runtime_window_metrics()
    vw, vh = (
        viewport
        if viewport is not None
        else (int(metrics["screen_width"]), int(metrics["screen_height"]))
    )
    proxy = resolve_system_http_proxy_for_steam()
    proxy_dict = {"server": proxy} if proxy else None
    ignore_default: list[str] = ["--enable-automation", "--no-sandbox"]
    launch_kwargs: dict = {
        "headless": headless,
        "proxy": proxy_dict,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=PasswordImport",
            "--window-position=0,0",
        ],
        "ignore_default_args": ignore_default,
    }
    ua = _resolved_playwright_user_agent()
    context_opts: dict = {
        "viewport": {"width": vw, "height": vh},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "extra_http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }
    if ua:
        context_opts["user_agent"] = ua
    if storage_state:
        context_opts["storage_state"] = str(Path(storage_state).resolve())

    for channel in order:
        try:
            browser = playwright.chromium.launch(channel=channel, **launch_kwargs)
            try:
                ctx = browser.new_context(**context_opts)
            except Exception:
                browser.close()
                raise
            attach_stealth_to_context(ctx)
            return browser, ctx
        except Exception as e:
            last_exc = e
            continue
    raise SteamBrowserNotFoundError(STEAM_BROWSER_NOT_INSTALLED_MSG) from last_exc
