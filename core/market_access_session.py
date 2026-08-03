"""Headed access-challenge windows for C5 / ECO collection gates.

When HTTP collection is blocked (client-version gate / slider), open the same
login browser profile in a visible window so the user can finish verification.
Successful sell-list XHRs from that window are reused for the rest of the run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from config import CACHE_DIR
from core.collection_cancel import CancelCheck, raise_if_cancelled
from core.steam.errors import SteamBrowserLaunchError, SteamBrowserNotFoundError
from core.steam.launch import focus_single_page, launch_persistent_chromium_context

ProgressCb = Callable[[str], None] | None


def _profile_dir(provider: str) -> Path:
    path = CACHE_DIR / "market_browser_profiles" / provider
    path.mkdir(parents=True, exist_ok=True)
    return path


def _filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    skip = {
        "host",
        "content-length",
        "connection",
        ":authority",
        ":method",
        ":path",
        ":scheme",
    }
    out: dict[str, str] = {}
    for key, value in headers.items():
        low = str(key).lower()
        if low in skip or low.startswith(":"):
            continue
        out[str(key)] = str(value)
    return out


@dataclass
class MarketAccessSession:
    provider: str
    _playwright: Any = field(default=None, repr=False)
    _context: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)
    _headers: dict[str, str] | None = field(default=None, repr=False)
    _opened: bool = False

    @property
    def display_name(self) -> str:
        return "C5GAME" if self.provider == "c5" else "ECOSteam"

    def open(self, *, progress: ProgressCb = None) -> None:
        if self._opened:
            return
        from playwright.sync_api import sync_playwright

        if progress:
            progress(
                f"请在弹出窗口完成 {self.display_name} 访问验证（滑块/安全检查）；"
                "静默采集时也会弹出此窗口…"
            )
        try:
            self._playwright = sync_playwright().start()
            self._context = launch_persistent_chromium_context(
                self._playwright,
                _profile_dir(self.provider),
                headless=False,
                viewport=None,
                webdriver_stealth=False,
                market_browser=True,
                isolated_profile=True,
            )
            self._page = focus_single_page(self._context)
            if self.provider == "c5":
                self._seed_c5_auth()
            self._opened = True
        except (SteamBrowserNotFoundError, SteamBrowserLaunchError) as exc:
            self.close()
            raise RuntimeError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise RuntimeError(
                f"无法打开 {self.display_name} 验证窗口：{exc}"
            ) from exc

    def close(self) -> None:
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        self._headers = None
        self._opened = False
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _seed_c5_auth(self) -> None:
        """Mirror APP-saved C5 cookie/token into the headed profile before XHR capture."""
        if self._context is None:
            return
        try:
            from core.market_candidates import load_c5_auth_for_browser
        except Exception:
            return
        cookie_header, token = load_c5_auth_for_browser()
        cookies: list[dict[str, Any]] = []
        if cookie_header:
            for part in str(cookie_header).split(";"):
                item = part.strip()
                if not item or "=" not in item:
                    continue
                name, value = item.split("=", 1)
                name = name.strip()
                if not name:
                    continue
                cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": ".c5game.com",
                        "path": "/",
                    }
                )
        if token:
            cookies.append(
                {
                    "name": "C5Token",
                    "value": token,
                    "domain": ".c5game.com",
                    "path": "/",
                }
            )
            cookies.append(
                {
                    "name": "access_token",
                    "value": token,
                    "domain": ".c5game.com",
                    "path": "/",
                }
            )
        if not cookies:
            return
        try:
            self._context.add_cookies(cookies)
        except Exception:
            pass

    def fetch_c5_list(
        self,
        *,
        item_id: int,
        min_wear: float,
        max_wear: float,
        page_no: int = 1,
        progress: ProgressCb = None,
        display_name: str = "",
        timeout_ms: int = 300_000,
        cancel_check: CancelCheck = None,
    ) -> dict[str, Any]:
        raise_if_cancelled(cancel_check)
        self.open(progress=progress)
        assert self._page is not None and self._context is not None
        label = display_name or "材料"
        if progress:
            progress(
                f"C5GAME · {label} · 验证窗口第 {page_no} 页（完成后会自动继续）"
            )
        list_url = (
            f"https://api.c5game.com/search/v2/sell/{item_id}/list"
            f"?page={page_no}&limit=20"
            f"&minWear={min_wear:.8f}&maxWear={max_wear:.8f}"
        )
        sell_page = (
            f"https://www.c5game.com/csgo/{item_id}/item/sell"
            f"?minWear={min_wear:.8f}&maxWear={max_wear:.8f}"
        )
        if self._headers:
            replay_headers = dict(self._headers)
            if not any(k.lower() == "accept-encoding" for k in replay_headers):
                replay_headers["Accept-Encoding"] = "gzip, br, zstd, deflate"
            response = self._context.request.get(
                list_url,
                headers=replay_headers,
                timeout=30_000,
            )
            raise_if_cancelled(cancel_check)
            if response.status == 429:
                raise RuntimeError("C5GAME 返回访问频率过高，已立即停止本平台采集")
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("C5GAME 在售接口返回异常")
            return payload

        captured_headers: dict[str, str] = {}
        latest_payload: dict[str, Any] | None = None

        def on_request(request) -> None:
            if f"/v2/sell/{item_id}/list" not in request.url:
                return
            if not captured_headers:
                captured_headers.update(
                    _filter_request_headers(dict(request.headers))
                )

        def on_response(response) -> None:
            nonlocal latest_payload
            if f"/v2/sell/{item_id}/list" not in response.url:
                return
            if response.request.method.upper() != "GET":
                return
            try:
                data = response.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            code = str(data.get("errorCode") or data.get("code") or "")
            if code == "102":
                return
            if data.get("success") is False and code not in {"", "0"}:
                return
            latest_payload = data

        page = focus_single_page(self._context, preferred=self._page)
        self._page = page

        def on_new_page(new_page) -> None:
            nonlocal page
            try:
                page = focus_single_page(self._context, preferred=new_page)
                self._page = page
            except Exception:
                pass

        self._context.on("page", on_new_page)
        page.on("request", on_request)
        page.on("response", on_response)
        try:
            page.goto(sell_page, wait_until="domcontentloaded", timeout=60_000)
            page = focus_single_page(self._context, preferred=page)
            self._page = page
            if "console-ban" in str(page.url or "").lower():
                raise RuntimeError(
                    "C5GAME 打开了开发者工具拦截页（console-ban）。"
                    "请关闭 F12/控制台后点「重新加载」，不要在验证窗口开开发者工具"
                )
            deadline = time.monotonic() + max(30.0, timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                raise_if_cancelled(cancel_check)
                page = focus_single_page(self._context, preferred=page)
                self._page = page
                if latest_payload is not None:
                    break
                href = str(page.url or "")
                if "console-ban" in href.lower():
                    raise RuntimeError(
                        "C5GAME 打开了开发者工具拦截页（console-ban）。"
                        "请关闭 F12/控制台后点「重新加载」，不要在验证窗口开开发者工具"
                    )
                if not self._context.pages:
                    raise RuntimeError("C5GAME 验证窗口已关闭，采集中断")
                page.wait_for_timeout(500)
            else:
                raise RuntimeError(
                    "等待 C5GAME 验证超时：请在弹出窗口完成安全检查后保持页面打开"
                )
        finally:
            try:
                self._context.remove_listener("page", on_new_page)
            except Exception:
                pass
            try:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass
        if captured_headers:
            self._headers = captured_headers
            try:
                from core.market_candidates import save_c5_client_headers

                save_c5_client_headers(captured_headers)
            except Exception:
                pass
        assert latest_payload is not None
        return latest_payload

    def fetch_eco_list(
        self,
        *,
        goods_id: int,
        min_wear: float = 0.0,
        max_wear: float = 1.0,
        page_no: int = 1,
        progress: ProgressCb = None,
        display_name: str = "",
        timeout_ms: int = 300_000,
        cancel_check: CancelCheck = None,
    ) -> str | dict[str, Any]:
        """Open ECO goods page; return HTML or SellGoodsQuery JSON when available.

        Slider verification is intermittent. When it does not appear, keep waiting
        for the sale table / API payload instead of asking the user to drag a
        non-existent slider.
        """
        raise_if_cancelled(cancel_check)
        self.open(progress=progress)
        assert self._page is not None and self._context is not None
        label = display_name or "材料"
        if progress:
            progress(f"ECOSteam · {label} · 正在打开商品页…")
        goods_url = (
            f"https://www.ecosteam.cn/goods/730-{quote(str(goods_id))}"
            f"-1-laypagesale-0-{max(1, int(page_no))}"
            f"-0-0-0-0-0-{float(min_wear):g}-{float(max_wear):g}"
            "-00-00-0-0-0.html"
        )
        page = self._page
        latest_payload: dict[str, Any] | None = None
        saw_slider = False

        def on_response(response) -> None:
            nonlocal latest_payload
            url = str(getattr(response, "url", "") or "")
            if "SellGoodsQuery" not in url:
                return
            try:
                data = response.json()
            except Exception:
                return
            if isinstance(data, dict):
                latest_payload = data

        page.on("response", on_response)
        try:
            page.goto(goods_url, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + max(30.0, timeout_ms / 1000.0)
            last_progress = 0.0
            while time.monotonic() < deadline:
                raise_if_cancelled(cancel_check)
                if latest_payload is not None:
                    return latest_payload
                href = str(page.url or "")
                now = time.monotonic()
                if "frequent_slider" in href.lower():
                    saw_slider = True
                    if progress and now - last_progress >= 2.0:
                        progress("ECOSteam · 请在弹出窗口拖动完成滑块验证…")
                        last_progress = now
                else:
                    html = page.content()
                    if (
                        "data-goodsnumber=" in html
                        or "data-saletradetype" in html.lower()
                    ):
                        return html
                    if progress and not saw_slider and now - last_progress >= 3.0:
                        progress(
                            f"ECOSteam · {label} · 商品页已打开，正在读取挂单"
                            "（若出现滑块请完成验证）…"
                        )
                        last_progress = now
                if not self._context.pages:
                    raise RuntimeError("ECOSteam 验证窗口已关闭，采集中断")
                page.wait_for_timeout(500)
            if saw_slider:
                raise RuntimeError(
                    "等待 ECOSteam 验证超时：请在弹出窗口完成滑块后保持商品页打开"
                )
            raise RuntimeError(
                "ECOSteam 商品页未出现挂单列表。"
                "请确认登录有效，或在弹出窗口手动刷新后再试"
            )
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    def complete_eco_access_gate(
        self,
        *,
        progress: ProgressCb = None,
        cancel_check: CancelCheck = None,
        timeout_ms: int = 180_000,
    ) -> None:
        """Open one headed window for slider/login, save cookies, then close.

        Used so silent collection can clear a gate without scraping every goods
        page in a visible browser.
        """
        raise_if_cancelled(cancel_check)
        self.open(progress=progress)
        assert self._page is not None and self._context is not None
        if progress:
            progress(
                "ECOSteam · 请在弹出窗口完成访问验证（有滑块就拖一下）；"
                "验证通过后窗口会自动关闭并继续静默采集…"
            )
        page = self._page
        page.goto(
            "https://www.ecosteam.cn/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        deadline = time.monotonic() + max(30.0, timeout_ms / 1000.0)
        last_progress = 0.0
        while time.monotonic() < deadline:
            raise_if_cancelled(cancel_check)
            href = str(page.url or "")
            now = time.monotonic()
            on_slider = "frequent_slider" in href.lower()
            if on_slider and progress and now - last_progress >= 2.0:
                progress("ECOSteam · 请在弹出窗口拖动完成滑块验证…")
                last_progress = now
            try:
                cookies = list(self._context.cookies())
            except Exception:
                cookies = []
            cookie = "; ".join(
                f"{item.get('name')}={item.get('value')}"
                for item in cookies
                if item.get("name") and item.get("value") is not None
            )
            has_refresh = "refreshtoken=" in cookie.lower()
            if has_refresh and not on_slider:
                try:
                    from core.market_candidates import save_eco_auth

                    save_eco_auth("", cookie)
                except Exception:
                    pass
                return
            if not self._context.pages:
                raise RuntimeError("ECOSteam 验证窗口已关闭，采集中断")
            page.wait_for_timeout(500)
        raise RuntimeError(
            "等待 ECOSteam 访问验证超时：请在弹出窗口完成滑块后保持页面打开"
        )


_SESSIONS: dict[str, MarketAccessSession] = {}


def get_access_session(provider: str) -> MarketAccessSession:
    key = str(provider or "").strip().lower()
    session = _SESSIONS.get(key)
    if session is None:
        session = MarketAccessSession(provider=key)
        _SESSIONS[key] = session
    return session


def close_access_sessions(*providers: str) -> None:
    keys = [str(p).strip().lower() for p in providers if str(p).strip()]
    if not keys:
        keys = list(_SESSIONS.keys())
    for key in keys:
        session = _SESSIONS.pop(key, None)
        if session is not None:
            session.close()
