"""Marketplace login capture and credential validation workers."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import sync_playwright

from config import CACHE_DIR
from core.market_candidates import (
    save_buff_auth,
    save_youpin_auth,
    validate_provider_login,
    validate_youpin_credentials,
)
from core.steam.launch import launch_persistent_chromium_context

_LOGIN_URLS = {
    "buff": "https://buff.163.com/account/login",
    "yyyp": "https://www.youpin898.com/",
}


def _cookie_header(cookies: list[dict[str, Any]], domain_hint: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip()
        if domain_hint not in domain or not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def _normalized_token(value: Any) -> str:
    token = str(value or "").strip()
    match = re.match(r"^Bearer\s+(.+)$", token, flags=re.IGNORECASE)
    if match:
        token = match.group(1).strip()
    if not (20 <= len(token) <= 4096):
        return ""
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", token, re.I):
        return ""
    return token


def _tokens_from_storage(raw: Any) -> list[str]:
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(value: Any, score: int) -> None:
        token = _normalized_token(value)
        if token and token not in seen:
            seen.add(token)
            candidates.append((score, token))

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 4:
            return
        score = 100 if re.search(r"token|authorization|auth", key, re.I) else 5
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key), depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, key, depth + 1)
        elif isinstance(value, str):
            stripped = value.strip()
            if (
                stripped[:1] not in "[{"
                and (
                    score >= 100
                    or re.fullmatch(
                        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                        stripped,
                    )
                )
            ):
                add(value, score)
            if value[:1] in "[{":
                try:
                    visit(json.loads(value), key, depth + 1)
                except ValueError:
                    pass

    visit(raw)
    candidates.sort(key=lambda item: (-item[0], -len(item[1])))
    return [token for _score, token in candidates]


class MarketplaceLoginValidationWorker(QThread):
    provider_checked = Signal(str, object)
    completed = Signal()

    def __init__(self, providers: list[str], parent=None) -> None:
        super().__init__(parent)
        self.providers = list(providers)

    def run(self) -> None:
        for provider in self.providers:
            result = validate_provider_login(provider)
            self.provider_checked.emit(provider, result)
        self.completed.emit()


class MarketplaceLoginCaptureWorker(QThread):
    """Open an app-owned browser profile and persist accepted credentials."""

    progress = Signal(str, str)
    completed = Signal(str, object)

    def __init__(self, provider: str, parent=None) -> None:
        super().__init__(parent)
        self.provider = str(provider)

    def run(self) -> None:
        provider = self.provider
        if provider not in _LOGIN_URLS:
            self.completed.emit(
                provider,
                {
                    "ok": False,
                    "message": "该平台暂不支持 APP 内登录捕获",
                },
            )
            return
        result: dict[str, Any] = {
            "ok": False,
            "message": "登录窗口已关闭，未捕获到有效登录凭证",
        }
        context = None
        try:
            with sync_playwright() as playwright:
                profile = (
                    Path(CACHE_DIR)
                    / "market_browser_profiles"
                    / provider
                )
                profile.mkdir(parents=True, exist_ok=True)
                context = launch_persistent_chromium_context(
                    playwright,
                    profile,
                    headless=False,
                    viewport=(1120, 780),
                )
                page = context.pages[0] if context.pages else context.new_page()
                if provider == "buff":
                    result = self._capture_buff(context, page)
                else:
                    result = self._capture_youpin(context, page)
        except Exception as exc:  # noqa: BLE001 - surface browser/runtime failures
            result = {
                "ok": False,
                "indeterminate": True,
                "message": f"登录窗口启动或捕获失败：{exc}",
            }
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
        self.completed.emit(provider, result)

    def _capture_buff(self, context, page) -> dict[str, Any]:
        self.progress.emit("buff", "请在 APP 打开的窗口中登录 BUFF…")
        page.goto(_LOGIN_URLS["buff"], wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            try:
                cookies = context.cookies()
                header = _cookie_header(cookies, "buff.163.com")
                if "session=" in header.lower():
                    save_buff_auth(header)
                    self.progress.emit("buff", "已捕获 BUFF Cookie，正在校验…")
                    return validate_provider_login("buff")
                if not context.pages:
                    break
                page.wait_for_timeout(800)
            except Exception:
                if not getattr(context, "pages", []):
                    break
                time.sleep(0.8)
        return {
            "ok": False,
            "message": "未捕获到 BUFF 登录 Cookie，请重新打开登录窗口",
        }

    def _capture_youpin(self, context, page) -> dict[str, Any]:
        self.progress.emit("yyyp", "请在 APP 打开的窗口中登录悠悠有品…")
        captured: list[str] = []
        tried: set[str] = set()

        def inspect_request(request) -> None:
            try:
                headers = request.headers
                token = _normalized_token(
                    headers.get("authorization")
                    or headers.get("Authorization")
                )
                if token and token not in captured:
                    captured.append(token)
            except Exception:
                pass

        context.on("request", inspect_request)
        page.goto(_LOGIN_URLS["yyyp"], wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            try:
                cookies = context.cookies()
                cookie = _cookie_header(cookies, "youpin898.com")
                for current_page in list(context.pages):
                    try:
                        storage = current_page.evaluate(
                            """() => {
                              const read = store => {
                                const out = {};
                                for (let i = 0; i < store.length; i++) {
                                  const key = store.key(i);
                                  out[key] = store.getItem(key);
                                }
                                return out;
                              };
                              return {
                                local: read(localStorage),
                                session: read(sessionStorage)
                              };
                            }"""
                        )
                        for token in _tokens_from_storage(storage):
                            if token not in captured:
                                captured.append(token)
                    except Exception:
                        continue
                while captured:
                    token = captured.pop(0)
                    if token in tried:
                        continue
                    tried.add(token)
                    self.progress.emit("yyyp", "已捕获 Token，正在核验用户资料…")
                    verified = validate_youpin_credentials(
                        token,
                        cookie,
                        timeout=8,
                    )
                    if verified.get("ok"):
                        save_youpin_auth(
                            token,
                            cookie,
                            nickname=str(verified.get("account_name") or ""),
                            user_id=verified.get("user_id"),
                        )
                        return validate_provider_login("yyyp")
                if not context.pages:
                    break
                page.wait_for_timeout(800)
            except Exception:
                if not getattr(context, "pages", []):
                    break
                time.sleep(0.8)
        return {
            "ok": False,
            "message": "未捕获到有效悠悠有品 Token，请重新打开登录窗口",
        }
