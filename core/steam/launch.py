"""Playwright 持久化上下文：目录解析、启动参数、stealth 注入。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from config import (
    PLAYWRIGHT_CHROME_USE_SYSTEM_USER_DATA,
    PLAYWRIGHT_CHROME_USER_DATA_DIR,
    PLAYWRIGHT_EDGE_USER_DATA_DIR,
    PLAYWRIGHT_MSEDGE_USE_SYSTEM_USER_DATA,
    PLAYWRIGHT_USER_AGENT,
)
from core.playwright_channel_prefs import load_playwright_channel_try_order
from .errors import (
    STEAM_BROWSER_NOT_INSTALLED_MSG,
    STEAM_BROWSER_PROFILE_BUSY_MSG,
    SteamBrowserLaunchError,
    SteamBrowserNotFoundError,
)
from .proxy import resolve_system_http_proxy_for_steam
from .stealth import playwright_stealth_init_js
from .window_metrics import get_runtime_window_metrics


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


def _is_browser_missing_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "executable doesn't exist",
        "executable not found",
        "browser has not been found",
        "browser type not found",
        "chrome not found",
        "msedge not found",
        "could not find native",
    )
    return any(marker in text for marker in markers)


def _is_profile_busy_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "singletonlock",
        "processsingleton",
        "profile is already in use",
        "user data directory is already in use",
        "the browser is already running",
        "being used by another process",
        "failed to create a processsingleton",
    )
    return any(marker in text for marker in markers)


def _clear_stale_profile_singletons(user_data_dir: Path) -> bool:
    """Best-effort cleanup of leftover Chromium singleton files after a crash."""
    cleared = False
    root = Path(user_data_dir)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = root / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
                cleared = True
        except OSError:
            continue
    return cleared


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
    market_browser: bool = False,
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
    args = [
        "--disable-features=PasswordImport",
        "--window-position=0,0",
    ]
    if market_browser:
        # Do NOT add --disable-blink-features=AutomationControlled:
        # Chrome shows an unsupported-flag banner, and C5 treats that as「开发者模式」.
        args.extend(
            [
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
            ]
        )
    opts: dict = {
        "user_data_dir": str(session_path),
        "headless": headless,
        "proxy": proxy_dict,
        "args": args,
        "ignore_default_args": ignore_default,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "extra_http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }
    if market_browser:
        # Fixed viewport makes outerHeight-innerHeight look like docked DevTools,
        # which triggers C5's /console-ban page.
        opts["no_viewport"] = True
    else:
        opts["viewport"] = {"width": vw, "height": vh}
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


def clear_chromium_session_restore(user_data_dir: Path) -> None:
    """Drop session/tab restore files so persistent profiles open a single blank page."""
    root = Path(user_data_dir)
    default = root / "Default"
    targets = [
        default / "Current Session",
        default / "Current Tabs",
        default / "Last Session",
        default / "Last Tabs",
        default / "Sessions",
        root / "Sessions",
    ]
    for path in targets:
        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def focus_single_page(context, preferred=None):
    """Keep one page in a persistent context; close tabs restored from the last session.

    Market login / access windows share a profile with collection. Without this,
    Chromium often reopens the previous C5/ECO tabs alongside the new login URL.
    """
    pages = list(getattr(context, "pages", []) or [])
    if preferred is not None and preferred in pages:
        page = preferred
    else:
        page = pages[0] if pages else context.new_page()
    for extra in pages:
        if extra is page:
            continue
        try:
            extra.close()
        except Exception:
            pass
    return page


def _raise_persistent_launch_failure(
    *,
    last_exc: Exception | None,
    saw_missing: bool,
    saw_busy: bool,
) -> None:
    # Prefer profile-busy over "not installed" — channels may fail for mixed reasons.
    if saw_busy:
        detail = _exception_text(last_exc) if last_exc else ""
        msg = STEAM_BROWSER_PROFILE_BUSY_MSG
        if detail:
            msg = f"{msg}（{detail}）"
        raise SteamBrowserLaunchError(msg) from last_exc
    if saw_missing and (last_exc is None or _is_browser_missing_error(last_exc)):
        raise SteamBrowserNotFoundError(STEAM_BROWSER_NOT_INSTALLED_MSG) from last_exc
    detail = _exception_text(last_exc) if last_exc else "未知错误"
    raise SteamBrowserLaunchError(f"启动浏览器失败：{detail}") from last_exc


def launch_persistent_chromium_context(
    playwright,
    session_path: Path,
    *,
    headless: bool,
    webdriver_stealth: bool = True,
    market_browser: bool = False,
    viewport: tuple[int, int] | None = None,
    channel_try_order: tuple[str, ...] | None = None,
    isolated_profile: bool = False,
):
    """按 ``channel_try_order`` 依次尝试 Playwright 渠道；未指定时从设置「首选浏览器」读取，再试另一种。

    ``isolated_profile=True`` 时强制使用 ``session_path``，忽略系统/环境浏览器数据目录
    （材料平台登录窗口应始终用独立目录，避免拖入完整 Chrome 配置导致启动极慢）。

    ``market_browser=True`` 时清理会话恢复、使用真实窗口尺寸（no_viewport），
    避免 C5 把固定 viewport 误判成「开了控制台」。
    """
    order = (
        channel_try_order
        if channel_try_order is not None
        else load_playwright_channel_try_order()
    )
    last_exc: Exception | None = None
    saw_missing = False
    saw_busy = False
    cleared_dirs: set[str] = set()

    def try_launch(channel: str | None) -> object | None:
        nonlocal last_exc, saw_missing, saw_busy
        data_dir = (
            Path(session_path).expanduser().resolve()
            if isolated_profile or channel is None
            else resolve_playwright_user_data_dir(session_path, channel)
        )
        if market_browser:
            clear_chromium_session_restore(data_dir)
        opts = persistent_launch_options(
            data_dir,
            headless=headless,
            viewport=None if market_browser else viewport,
            market_browser=market_browser,
        )
        for attempt in range(2):
            try:
                launch_kw = dict(opts)
                if channel:
                    ctx = playwright.chromium.launch_persistent_context(
                        channel=channel, **launch_kw
                    )
                else:
                    ctx = playwright.chromium.launch_persistent_context(**launch_kw)
                # Skip JS stealth for market windows: init scripts + outer* patches
                # are easier for C5 to spot than a plain Chrome window.
                if webdriver_stealth and not market_browser:
                    attach_stealth_to_context(ctx)
                return ctx
            except Exception as e:
                last_exc = e
                if _is_browser_missing_error(e):
                    saw_missing = True
                    return None
                if _is_profile_busy_error(e):
                    saw_busy = True
                    key = str(data_dir)
                    if attempt == 0 and key not in cleared_dirs:
                        cleared_dirs.add(key)
                        if _clear_stale_profile_singletons(data_dir):
                            continue
                    return None
                # Non-missing launch failure: still try next channel / bundled chromium
                return None
        return None

    for channel in order:
        ctx = try_launch(channel)
        if ctx is not None:
            return ctx

    # System Edge/Chrome channels missing or broken: fall back to Playwright Chromium.
    ctx = try_launch(None)
    if ctx is not None:
        return ctx

    _raise_persistent_launch_failure(
        last_exc=last_exc, saw_missing=saw_missing, saw_busy=saw_busy
    )
    raise AssertionError("unreachable")


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
    saw_missing = False
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

    def try_launch(channel: str | None):
        nonlocal last_exc, saw_missing
        try:
            if channel:
                browser = playwright.chromium.launch(channel=channel, **launch_kwargs)
            else:
                browser = playwright.chromium.launch(**launch_kwargs)
            try:
                ctx = browser.new_context(**context_opts)
            except Exception:
                browser.close()
                raise
            attach_stealth_to_context(ctx)
            return browser, ctx
        except Exception as e:
            last_exc = e
            if _is_browser_missing_error(e):
                saw_missing = True
            return None

    for channel in order:
        pair = try_launch(channel)
        if pair is not None:
            return pair

    pair = try_launch(None)
    if pair is not None:
        return pair

    if saw_missing and (last_exc is None or _is_browser_missing_error(last_exc)):
        raise SteamBrowserNotFoundError(STEAM_BROWSER_NOT_INSTALLED_MSG) from last_exc
    detail = _exception_text(last_exc) if last_exc else "未知错误"
    raise SteamBrowserLaunchError(f"启动浏览器失败：{detail}") from last_exc
