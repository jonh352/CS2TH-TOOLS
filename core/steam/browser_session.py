"""Steam 登录态：轻量 ``storage_state`` 持久化 + 临时目录交互登录；取 Cookie 走非持久化 Browser。

与数据采集共用的账号根目录（如 ``STEAM_SESSION_DIR``）内仅保留 ``steam_storage_state.json``
与可清理的 ``_login_session/``，不再长期占用完整 Playwright profile 作为 Steam 会话载体。

``PLAYWRIGHT_PROFILE_LOCK`` 供各模块串行访问 Playwright。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import sync_playwright

from config import (
    STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S,
    STEAM_BROWSER_LOGIN_COMPLETE_WAIT_S,
    STEAM_BROWSER_LOGIN_PAGE_TIMEOUT_S,
    STEAM_BROWSER_MY_PROFILE_TIMEOUT_S,
    STEAM_BROWSER_PROFILE_BY_STEAM_ID_TIMEOUT_S,
    STEAM_BROWSER_PROFILE_SELECTOR_TIMEOUT_S,
)

from core.steam_session_profiles import (
    steam_account_storage_state_path,
    steam_account_temp_login_session_dir,
)

from .constants import DEFAULT_SESSION_DIR
from .cookie_header import build_steam_inventory_cookie_header
from .launch import launch_ephemeral_chromium_context, launch_persistent_chromium_context
from .models import SteamWebProfile
from .profile import cache_steam_avatar, extract_steam_profile_from_page

PLAYWRIGHT_PROFILE_LOCK = threading.RLock()


def _steam_browser_timeout_ms(seconds: float) -> int:
    return max(1, int(float(seconds) * 1000))


def _session_path(session_dir: Path | str | None) -> Path:
    return Path(session_dir) if session_dir is not None else DEFAULT_SESSION_DIR


def _say(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)
    else:
        print(msg)


def _storage_state_file_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        if path.stat().st_size < 32:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("cookies"), list)


def _cleanup_temp_login_dir(temp_session_dir: Path) -> None:
    if temp_session_dir.is_dir():
        shutil.rmtree(temp_session_dir, ignore_errors=True)


def _prepare_temp_login_dir(temp_session_dir: Path) -> None:
    _cleanup_temp_login_dir(temp_session_dir)
    temp_session_dir.mkdir(parents=True, exist_ok=True)


def _resolve_steam_profile_post_home(
    page,
    say: Callable[[str], None],
    *,
    profile_from_landing: SteamWebProfile | None,
    known_steam_id: str,
    prefer_known_steam_url: bool,
) -> SteamWebProfile | None:
    ks = (known_steam_id or "").strip()
    if prefer_known_steam_url and ks and re.fullmatch(r"\d{17}", ks):
        say("正在同步 Steam 会话...")
        try:
            page.goto(
                f"https://steamcommunity.com/profiles/{ks}/",
                wait_until="load",
                timeout=_steam_browser_timeout_ms(STEAM_BROWSER_PROFILE_BY_STEAM_ID_TIMEOUT_S),
            )
        except Exception:
            pass
        if "/login" in page.url:
            return None
        time.sleep(0.35)
        profile = extract_steam_profile_from_page(page)
        if not profile:
            profile = SteamWebProfile(steam_id=ks, personaname="", avatar_url="")
        return profile

    if profile_from_landing and re.fullmatch(
        r"\d{17}", str(profile_from_landing.steam_id).strip()
    ):
        say("正在读取 Steam 个人资料...")
        time.sleep(0.25)
        return profile_from_landing

    say("正在读取 Steam 个人资料...")
    try:
        page.goto(
            "https://steamcommunity.com/my/profile",
            wait_until="domcontentloaded",
            timeout=_steam_browser_timeout_ms(STEAM_BROWSER_MY_PROFILE_TIMEOUT_S),
        )
        try:
            page.wait_for_selector(
                ".playerAvatar img, .playerAvatarAutoSizeInner img, [data-miniprofile]",
                timeout=_steam_browser_timeout_ms(STEAM_BROWSER_PROFILE_SELECTOR_TIMEOUT_S),
            )
        except Exception:
            pass
        time.sleep(0.35)
    except Exception:
        say("⚠️ 无法打开 Steam 个人资料页")
        return None
    return extract_steam_profile_from_page(page)


def login_steam_session_to_storage_state(
    state_path: Path,
    temp_session_dir: Path,
    on_status: Optional[Callable[[str], None]] = None,
) -> SteamWebProfile:
    """
    使用临时持久化目录完成人工登录，导出 ``context.storage_state`` 到 ``state_path``，
    随后关闭浏览器并删除临时目录。
    """
    def say(msg: str) -> None:
        _say(on_status, msg)

    state_path = state_path.resolve()
    temp_session_dir = temp_session_dir.resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if state_path.is_file():
        try:
            state_path.unlink()
        except OSError:
            pass

    _prepare_temp_login_dir(temp_session_dir)

    profile: SteamWebProfile | None = None
    try:
        with sync_playwright() as p:
            say(
                f"👉 正在打开浏览器，请手动登录 Steam（{STEAM_BROWSER_LOGIN_COMPLETE_WAIT_S}秒内）..."
            )
            context = launch_persistent_chromium_context(
                p, temp_session_dir, headless=False
            )
            try:
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                for pg in list(context.pages):
                    if pg != page:
                        pg.close()

                page.goto(
                    "https://steamcommunity.com/login",
                    wait_until="load",
                    timeout=_steam_browser_timeout_ms(STEAM_BROWSER_LOGIN_PAGE_TIMEOUT_S),
                )
                try:
                    page.wait_for_url(
                        lambda u: "/login" not in str(u),
                        timeout=_steam_browser_timeout_ms(STEAM_BROWSER_LOGIN_COMPLETE_WAIT_S),
                    )
                except Exception:
                    say("⏱️ 等待超时...")
                profile_from_landing: SteamWebProfile | None = None
                if "/login" not in page.url:
                    time.sleep(0.4)
                    profile_from_landing = extract_steam_profile_from_page(page)

                page.goto(
                    "https://steamcommunity.com/",
                    wait_until="load",
                    timeout=_steam_browser_timeout_ms(STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S),
                )
                if "/login" in page.url:
                    say("⚠️ 未完成 Steam 登录")
                    raise RuntimeError("Steam 登录未完成或已取消")

                profile = _resolve_steam_profile_post_home(
                    page,
                    say,
                    profile_from_landing=profile_from_landing,
                    known_steam_id="",
                    prefer_known_steam_url=False,
                )
                if not profile:
                    say("⚠️ 无法读取 Steam 账号信息，请重试")
                    raise RuntimeError("Steam 登录未完成或已取消")

                say(f"✅ 当前账号: {profile.personaname}")
                profile.avatar_local_path = cache_steam_avatar(
                    profile.avatar_url, profile.steam_id
                )

                context.storage_state(path=str(state_path))
            finally:
                try:
                    context.close()
                except Exception:
                    pass

        if not _storage_state_file_usable(state_path):
            raise RuntimeError("未能写入 Steam 登录状态，请重试")

        assert profile is not None
        return profile
    except Exception:
        try:
            if state_path.is_file():
                state_path.unlink()
        except OSError:
            pass
        raise
    finally:
        _cleanup_temp_login_dir(temp_session_dir)


def get_steam_cookies_from_storage_state(
    state_path: Path,
    on_status: Optional[Callable[[str], None]] = None,
    *,
    known_steam_id: str = "",
) -> tuple[str | None, SteamWebProfile | None]:
    """用非持久化 Browser + ``storage_state`` 恢复会话并导出库存 Cookie。"""
    def say(msg: str) -> None:
        _say(on_status, msg)

    state_path = state_path.resolve()
    if not _storage_state_file_usable(state_path):
        return None, None

    ks = (known_steam_id or "").strip()
    prefer_known = bool(ks and re.fullmatch(r"\d{17}", ks))

    with sync_playwright() as p:
        browser = None
        context = None
        try:
            say("✅ 已登录，静默获取 Cookie...")
            browser, context = launch_ephemeral_chromium_context(
                p,
                headless=True,
                storage_state=state_path,
            )
            page = context.new_page()
            page.goto(
                "https://steamcommunity.com/",
                wait_until="load",
                timeout=_steam_browser_timeout_ms(STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S),
            )
            if "/login" in page.url:
                say("Steam 会话已失效，请重新登录")
                return None, None

            profile = _resolve_steam_profile_post_home(
                page,
                say,
                profile_from_landing=None,
                known_steam_id=known_steam_id,
                prefer_known_steam_url=prefer_known,
            )
            if not profile:
                say("⚠️ 无法读取 Steam 账号信息")
                return None, None

            say(f"✅ 当前账号: {profile.personaname}")
            profile.avatar_local_path = cache_steam_avatar(
                profile.avatar_url, profile.steam_id
            )

            cookie_header = build_steam_inventory_cookie_header(
                context, profile.steam_id
            )
            if not cookie_header.strip():
                say("⚠️ 未读取到 Steam Cookie，请重新登录")
                return None, None
            say("✅ 已获取 Cookie")
            return cookie_header, profile
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def get_steam_cookies(
    session_dir: Path | str | None = None,
    on_status: Optional[Callable[[str], None]] = None,
    *,
    allow_interactive_login: bool = True,
    known_steam_id: str = "",
) -> tuple[str | None, SteamWebProfile | None]:
    """
    返回 (Cookie 请求头, 资料)。
    优先从 ``steam_storage_state.json`` 恢复；失败且 ``allow_interactive_login`` 时再走交互登录。
    """
    session_path = _session_path(session_dir)
    state_path = steam_account_storage_state_path(session_path)
    temp_dir = steam_account_temp_login_session_dir(session_path)

    with PLAYWRIGHT_PROFILE_LOCK:
        return _get_steam_cookies_impl(
            session_path,
            state_path,
            temp_dir,
            on_status,
            allow_interactive_login=allow_interactive_login,
            known_steam_id=known_steam_id,
        )


def _get_steam_cookies_impl(
    session_path: Path,
    state_path: Path,
    temp_dir: Path,
    on_status: Optional[Callable[[str], None]] = None,
    *,
    allow_interactive_login: bool = True,
    known_steam_id: str = "",
) -> tuple[str | None, SteamWebProfile | None]:
    def say(msg: str) -> None:
        _say(on_status, msg)

    cookie, profile = get_steam_cookies_from_storage_state(
        state_path,
        on_status,
        known_steam_id=known_steam_id,
    )
    if cookie is not None and profile is not None:
        return cookie, profile

    if not allow_interactive_login:
        say("未登录 Steam，请先点击「登录 Steam」完成登录")
        return None, None

    say(
        f"👉 未登录或会话失效，正在打开浏览器，请手动登录 Steam（{STEAM_BROWSER_LOGIN_COMPLETE_WAIT_S}秒内）..."
    )
    login_steam_session_to_storage_state(
        state_path=state_path,
        temp_session_dir=temp_dir,
        on_status=on_status,
    )
    return get_steam_cookies_from_storage_state(
        state_path,
        on_status,
        known_steam_id=known_steam_id,
    )


def clear_steam_session(session_dir: Path | str | None = None) -> None:
    """清除 Steam 轻量登录态与临时交互目录；不删除账号根目录下其它文件（如采集站点的持久化 profile）。"""
    path = _session_path(session_dir)
    ss = steam_account_storage_state_path(path)
    if ss.is_file():
        try:
            ss.unlink()
        except OSError:
            pass
    tmp = steam_account_temp_login_session_dir(path)
    _cleanup_temp_login_dir(tmp)


def login_steam_session(
    session_dir: Path | str,
    on_status: Optional[Callable[[str], None]] = None,
) -> SteamWebProfile:
    """交互登录并写入 ``steam_storage_state.json``（``session_dir`` 为账号根目录）。"""
    root = Path(session_dir).resolve()
    state_path = steam_account_storage_state_path(root)
    temp_dir = steam_account_temp_login_session_dir(root)
    with PLAYWRIGHT_PROFILE_LOCK:
        profile = login_steam_session_to_storage_state(
            state_path=state_path,
            temp_session_dir=temp_dir,
            on_status=on_status,
        )
    if on_status:
        on_status("✅ Steam 登录已保存")
    return profile
