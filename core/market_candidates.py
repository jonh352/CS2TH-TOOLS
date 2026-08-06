"""Low-frequency exact-wear listing collection for trade-up candidates."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

import requests

from config import CACHE_DIR
from core.collection_cancel import (
    CancelCheck,
    CollectionCancelled,
    interruptible_wait,
    raise_if_cancelled,
)
from core.data_utils import SkinTemplate

logger = logging.getLogger(__name__)


class EcoAccessGateError(RuntimeError):
    """ECO listing API returned an access-gate / rate-limit response."""

    def __init__(
        self,
        message: str,
        *,
        result_msg: str = "",
        needs_slider: bool = False,
    ) -> None:
        super().__init__(message)
        self.result_msg = str(result_msg or "")
        self.needs_slider = bool(needs_slider)


class EcoPlatformPausedError(RuntimeError):
    """ECO access still blocked; pause the platform for the rest of this run."""

    pass


class C5AccessGateError(RuntimeError):
    """C5 listing APIs are blocked (rate-limit / risk / security check)."""

    def __init__(
        self,
        message: str,
        *,
        needs_verify: bool = False,
    ) -> None:
        super().__init__(message)
        self.needs_verify = bool(needs_verify)


class C5PlatformPausedError(RuntimeError):
    """C5 access still blocked; pause the platform for the rest of this run."""

    pass

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BUFF_API = "https://buff.163.com/api/market/goods/sell_order"
# BUFF accepts price.asc / price.desc on sell_order; use ascending so 2× unit-price early stop is valid.
_BUFF_SELL_ORDER_SORT_BY = "price.asc"
_BUFF_SELL_ORDER_PAGE_SIZE = 50
_BUFF_LOGIN_CHECK_GOODS_ID = 956527
# Wear-filtered listing queries require a real login on every platform.
_LOGIN_CHECK_MIN_WEAR = 0.07
_LOGIN_CHECK_MAX_WEAR = 0.15
_YOUPIN_LOGIN_CHECK_TEMPLATE_ID = 125007
_C5_LOGIN_CHECK_ITEM_ID = 1098059387020423168
_C5_SELL_LIST_PAGE_SIZE = 40
# C5 website sell list orderBy: 0 default, 2 price asc, 3 price desc.
_C5_SELL_LIST_ORDER_BY_PRICE_ASC = 2
_ECO_LOGIN_CHECK_GOODS_ID = 7332
_YOUPIN_API = (
    "https://api.youpin898.com/api/homepage/es/commodity/GetCsGoPagedList"
)
_YOUPIN_USER_INFO_API = (
    "https://api.youpin898.com/api/user/Account/GetUserInfo"
)
_YOUPIN_USER_INFO_APIS = (
    _YOUPIN_USER_INFO_API,
    "https://api.youpin898.com/api/user/Account/getUserInfo",
)
EXACT_WEAR_PROVIDERS = frozenset({"buff", "yyyp", "c5", "eco"})
# Platforms that store APP-owned credentials and support real login checks.
APP_LOGIN_PROVIDERS = frozenset({"buff", "yyyp", "c5", "eco"})
_C5_USER_CHECK_APIS = (
    "https://api.c5game.com/common/store/v1/user",
    "https://api.c5game.com/balance/user/account/v1/money",
    "https://api.c5game.com/account/v1/my/account",
    "https://api.c5game.com/account/v1/my/balance",
)
_C5_SELL_LIST_API = (
    "https://www.c5game.com/api/v1/search/v2/sell/{item_id}/list"
)
# Website napi used by public scrapers (e.g. SteamTradingSiteTracker); needs login cookie.
_C5_NAPI_SELL_LIST_API = (
    "https://www.c5game.com/napi/trade/steamtrade/sga/sell/v3/list"
)
_C5_OPENAPI_BASE = "https://openapi.c5game.com"
_C5_OPENAPI_PRODUCTS_SEARCH = f"{_C5_OPENAPI_BASE}/merchant/market/v2/products/search"
_C5_OPENAPI_BALANCE = f"{_C5_OPENAPI_BASE}/merchant/account/v1/balance"
_C5_OPENAPI_AUTH_FILE = "c5_openapi.json"
_C5_CLIENT_HEADERS_FILE = "c5_client_headers.json"
# Do not hardcode a stale App-Version — C5 returns error 102 when it is too old.
_C5_DEFAULT_CLIENT_HEADERS = {
    "x-app-channel": "WEB",
    "x-source": "1",
    "x-area": "1",
}
# Required by C5 since 2025-06-17; urllib3 defaults omit zstd and get blocked.
_C5_ACCEPT_ENCODING = "gzip, br, zstd, deflate"
_ECO_USER_INFO_APIS = (
    "https://api.ecosteam.cn/Api/User/GetUserInfo",
    "https://www.ecosteam.cn/Api/User/GetUserInfo",
    "https://api.ecosteam.cn/api/User/GetUserInfo",
)
# Website sell-order query used by ECO goods pages (GoodsId or HashName + wear).
_ECO_SELL_QUERY_APIS = (
    "https://api.ecosteam.cn/Api/SteamGoods/SellGoodsQuery",
    "https://www.ecosteam.cn/Api/SteamGoods/SellGoodsQuery",
)
_PROVIDER_DISPLAY_NAMES = {
    "buff": "BUFF",
    "yyyp": "悠悠有品",
    "c5": "C5GAME",
    "eco": "ECOSteam",
}
_CANDIDATE_CACHE_TTL_SECONDS = 180.0
# ``max_pages <= 0`` means collect until the provider reports its last page.
# Keep a high emergency ceiling so a broken endpoint that repeats one page can
# never loop forever.
_AUTO_PAGE_SAFETY_LIMIT = 500
_candidate_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}


def _effective_page_limit(max_pages: int) -> int:
    try:
        value = int(max_pages)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else _AUTO_PAGE_SAFETY_LIMIT


def provider_display_name(provider: str) -> str:
    key = str(provider or "").strip().lower()
    return _PROVIDER_DISPLAY_NAMES.get(key, key or "平台")

_WEAR_BOUNDS = {
    "崭新出厂": (0.0, 0.07),
    "略有磨损": (0.07, 0.15),
    "久经沙场": (0.15, 0.38),
    "破损不堪": (0.38, 0.45),
    "战痕累累": (0.45, 1.0),
}
# BUFF / 悠悠 / C5 stop paging once listings exceed this multiple of recipe unit_price_cny.
_COLLECTION_PRICE_CAP_MULTIPLIER = 2.0
# ECO uses a slightly looser cap after switching to price-asc Sort.
_COLLECTION_ECO_PRICE_CAP_MULTIPLIER = 2.5
# Per platform · per material · per wear window (e.g. 略磨 / 久经).
_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW = 300


def _collection_window_row_limit_reached(kept: int) -> bool:
    return int(kept) >= _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW


def _collection_max_unit_price(
    unit_price_cny: Any,
    *,
    multiplier: float | None = None,
) -> float | None:
    """Return recipe unit price × multiplier, or None when unusable."""
    try:
        value = float(unit_price_cny or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    factor = (
        float(multiplier)
        if multiplier is not None
        else float(_COLLECTION_PRICE_CAP_MULTIPLIER)
    )
    if factor <= 0:
        return None
    return value * factor


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _app_auth_path(filename: str) -> Path:
    return CACHE_DIR / "market_auth" / filename


def _write_auth_json(filename: str, payload: dict[str, Any]) -> None:
    path = _app_auth_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _auth_file_candidates(filename: str) -> list[Path]:
    paths = [CACHE_DIR / "market_auth" / filename]
    configured = os.getenv("CS2TH_TOOL_DATA_DIR", "").strip()
    if configured:
        paths.append(Path(configured) / filename)
    # Development/shared-suite location. Packaged builds can set
    # CS2TH_TOOL_DATA_DIR to the terminal helper's data directory.
    paths.append(Path(r"D:\CS2TH-TOOL\data") / filename)
    return paths


def save_buff_auth(cookie: str) -> dict[str, Any]:
    cookie = str(cookie or "").strip()
    ok = "session=" in cookie.lower()
    payload = {
        "cookie": cookie if ok else "",
        "ok": ok,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_auth_json("buff_cookie.json", payload)
    return payload


def save_youpin_auth(
    token: str,
    cookie: str = "",
    *,
    nickname: str = "",
    user_id: Any = None,
) -> dict[str, Any]:
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    nickname = str(nickname or "").strip()
    ok = bool(token and nickname and user_id not in (None, ""))
    payload = {
        "token": token if ok else "",
        "cookie": cookie if ok else "",
        "nickname": nickname if ok else "",
        "user_id": user_id if ok else None,
        "ok": ok,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_auth_json("youpin_auth.json", payload)
    return payload


def save_c5_auth(
    cookie: str,
    token: str = "",
    *,
    nickname: str = "",
    user_id: Any = None,
) -> dict[str, Any]:
    cookie = str(cookie or "").strip()
    token = str(token or "").strip() or _cookie_value(
        cookie, "NC5_accessToken", "C5Token", "access_token"
    )
    nickname = str(nickname or "").strip()
    ok = bool(
        token
        or _cookie_looks_authenticated(
            cookie,
            ("nc5_accesstoken", "c5token", "access_token", "ncaccess", "token"),
        )
    )
    payload = {
        "cookie": cookie if ok else "",
        "token": token if ok else "",
        "nickname": nickname if ok else "",
        "user_id": user_id if ok else None,
        "ok": ok,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_auth_json("c5_auth.json", payload)
    return payload


def save_c5_openapi_auth(
    app_key: str,
    app_secret: str = "",
) -> dict[str, Any]:
    """Persist C5 Open Platform credentials (app-key) for official sell-list APIs."""
    app_key = str(app_key or "").strip()
    app_secret = str(app_secret or "").strip()
    ok = len(app_key) >= 16
    payload = {
        "app_key": app_key if ok else "",
        "app_secret": app_secret if ok else "",
        "ok": ok,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_auth_json(_C5_OPENAPI_AUTH_FILE, payload)
    return payload


def load_c5_openapi_auth() -> tuple[str, str]:
    """Return ``(app_key, app_secret)`` saved for C5 OpenAPI collection."""
    return _c5_openapi_auth()


def save_eco_auth(
    token: str,
    cookie: str = "",
    *,
    nickname: str = "",
    user_id: Any = None,
) -> dict[str, Any]:
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    nickname = str(nickname or "").strip()
    ok = bool(token) or _cookie_looks_authenticated(
        cookie,
        (
            "token",
            "authorization",
            "eco_token",
            "access_token",
            "refreshtoken",
        ),
    )
    payload = {
        "token": token if ok else "",
        "cookie": cookie if ok else "",
        "nickname": nickname if ok else "",
        "user_id": user_id if ok else None,
        "ok": ok,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_auth_json("eco_auth.json", payload)
    return payload


def _cookie_looks_authenticated(cookie: str, keys: tuple[str, ...]) -> bool:
    lower = str(cookie or "").lower()
    if not lower:
        return False
    for key in keys:
        needle = f"{key.lower()}="
        if needle not in lower:
            continue
        start = lower.find(needle) + len(needle)
        end = lower.find(";", start)
        value = lower[start:] if end < 0 else lower[start:end]
        if len(value.strip()) >= 8:
            return True
    return False


def clear_provider_auth(provider: str) -> dict[str, Any]:
    """Forget credentials and the app-owned browser profile for one provider.

    A local tombstone intentionally takes precedence over the optional
    CS2TH-TOOL shared credential fallback, so “clear” has an immediate and
    unambiguous effect without deleting data owned by another application.
    """
    provider = str(provider or "").strip().lower()
    filenames = {
        "buff": "buff_cookie.json",
        "yyyp": "youpin_auth.json",
        "c5": "c5_auth.json",
        "eco": "eco_auth.json",
    }
    filename = filenames.get(provider)
    if filename is None:
        return {"ok": False, "error": "该平台没有可由本 APP 清除的登录凭证"}
    _write_auth_json(
        filename,
        {
            "ok": False,
            "cleared": True,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    if provider == "c5":
        for extra in (_C5_CLIENT_HEADERS_FILE, _C5_OPENAPI_AUTH_FILE):
            try:
                _app_auth_path(extra).unlink(missing_ok=True)
            except OSError:
                pass
        _write_auth_json(
            _C5_OPENAPI_AUTH_FILE,
            {
                "ok": False,
                "cleared": True,
                "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
    _candidate_cache.clear()

    profiles_root = (CACHE_DIR / "market_browser_profiles").resolve()
    profile = (profiles_root / provider).resolve()
    profile_removed = False
    profile_error = ""
    if profile.parent == profiles_root and profile.is_dir():
        try:
            shutil.rmtree(profile)
            profile_removed = True
        except OSError as exc:
            profile_error = str(exc)
    return {
        "ok": True,
        "provider": provider,
        "profile_removed": profile_removed,
        "profile_error": profile_error,
    }


def clear_c5_session_auth() -> dict[str, Any]:
    """Clear C5 login cookie/token and browser session; keep client device-id headers."""
    _write_auth_json(
        "c5_auth.json",
        {
            "ok": False,
            "cleared": True,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    _candidate_cache.clear()

    profiles_root = (CACHE_DIR / "market_browser_profiles").resolve()
    profile = (profiles_root / "c5").resolve()
    profile_removed = False
    profile_error = ""
    if profile.parent == profiles_root and profile.is_dir():
        try:
            shutil.rmtree(profile)
            profile_removed = True
        except OSError as exc:
            profile_error = str(exc)
    return {
        "ok": True,
        "provider": "c5",
        "profile_removed": profile_removed,
        "profile_error": profile_error,
    }


def _buff_cookie() -> str:
    for path in _auth_file_candidates("buff_cookie.json"):
        data = _read_json(path)
        if data.get("cleared"):
            return ""
        value = str(data.get("cookie") or "").strip()
        if value:
            return value
    return ""


def _youpin_auth() -> tuple[str, str]:
    for path in _auth_file_candidates("youpin_auth.json"):
        data = _read_json(path)
        if data.get("cleared"):
            return "", ""
        token = str(data.get("token") or "").strip()
        cookie = str(data.get("cookie") or "").strip()
        if token or cookie:
            return token, cookie
    return "", ""


def _c5_auth() -> tuple[str, str]:
    for path in _auth_file_candidates("c5_auth.json"):
        data = _read_json(path)
        if data.get("cleared"):
            return "", ""
        cookie = str(data.get("cookie") or "").strip()
        token = str(data.get("token") or "").strip()
        if cookie or token:
            return cookie, token
    return "", ""


def _c5_openapi_auth() -> tuple[str, str]:
    for path in _auth_file_candidates(_C5_OPENAPI_AUTH_FILE):
        data = _read_json(path)
        if data.get("cleared"):
            return "", ""
        app_key = str(data.get("app_key") or data.get("app-key") or "").strip()
        app_secret = str(data.get("app_secret") or data.get("app-secret") or "").strip()
        if app_key:
            return app_key, app_secret
    return "", ""


def load_c5_auth_for_browser() -> tuple[str, str]:
    """Public helper for headed access windows that need APP-saved C5 auth."""
    return _c5_auth()


def save_c5_client_headers(headers: dict[str, Any]) -> None:
    """Persist browser-captured C5 client markers so later HTTP collection can reuse them.

    App-Version is intentionally not saved: website API returns error 102 for common
    captured values, and collection no longer sends that header.
    """
    if not isinstance(headers, dict):
        return
    wanted = {
        "user-agent",
        "x-app-channel",
        "x-source",
        "x-area",
        "x-traffic-tag",
        "x-device-id",
        "x-device-os",
        "x-device-model",
    }
    out: dict[str, str] = {}
    for key, value in headers.items():
        low = str(key).lower()
        text = str(value or "").strip()
        if low in wanted and text:
            if low == "user-agent":
                out["User-Agent"] = text
            elif low.startswith("x-"):
                out[low] = text
    if not out:
        return
    payload = _read_json(_app_auth_path(_C5_CLIENT_HEADERS_FILE))
    if not isinstance(payload, dict):
        payload = {}
    payload.update(out)
    # Drop any previously persisted version that would break sell list calls.
    payload.pop("App-Version", None)
    payload.pop("app-version", None)
    payload["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_auth_json(_C5_CLIENT_HEADERS_FILE, payload)


def _c5_client_headers() -> dict[str, str]:
    merged = dict(_C5_DEFAULT_CLIENT_HEADERS)
    data = _read_json(_app_auth_path(_C5_CLIENT_HEADERS_FILE))
    if isinstance(data, dict):
        for key in (
            "x-app-channel",
            "x-source",
            "x-area",
            "x-traffic-tag",
            "x-device-id",
            "x-device-os",
            "x-device-model",
            "User-Agent",
        ):
            value = str(data.get(key) or "").strip()
            if value:
                merged[key] = value
        # Tolerate lowercase keys from older dumps.
        for src, dst in (
            ("app-version", "App-Version"),
            ("user-agent", "User-Agent"),
        ):
            value = str(data.get(src) or "").strip()
            if value and dst not in merged:
                merged[dst] = value
    return merged


def _cookie_value(cookie: str, *names: str) -> str:
    """Return a cookie value by name without depending on its original casing."""
    wanted = {str(name).strip().lower() for name in names if str(name).strip()}
    for part in str(cookie or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.strip().lower() in wanted:
            return value.strip()
    return ""


def _c5_effective_token(cookie: str, token: str) -> str:
    """C5 web login currently stores its bearer token in NC5_accessToken."""
    return str(token or "").strip() or _cookie_value(
        cookie,
        "NC5_accessToken",
        "C5Token",
        "access_token",
    )


def _eco_auth() -> tuple[str, str]:
    for path in _auth_file_candidates("eco_auth.json"):
        data = _read_json(path)
        if data.get("cleared"):
            return "", ""
        token = str(data.get("token") or "").strip()
        cookie = str(data.get("cookie") or "").strip()
        if token or cookie:
            return token, cookie
    return "", ""


def provider_auth_available(provider: str) -> bool:
    if provider == "buff":
        return "session=" in _buff_cookie().lower()
    if provider == "yyyp":
        token, _cookie = _youpin_auth()
        return bool(token)
    if provider == "c5":
        # Shipped app path: website login cookie/token. OpenAPI is not required.
        cookie, token = _c5_auth()
        return bool(token) or _cookie_looks_authenticated(
            cookie, ("c5token", "access_token", "ncaccess", "token")
        )
    if provider == "eco":
        token, cookie = _eco_auth()
        return bool(token) or _cookie_looks_authenticated(
            cookie,
            (
                "token",
                "authorization",
                "eco_token",
                "access_token",
                "refreshtoken",
            ),
        )
    return False


def validate_provider_login(
    provider: str,
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Validate credentials used by this app, instead of browser login hints.

    ``ok`` is true only after the platform accepts the credential.  A
    rate-limit/network failure is reported as ``indeterminate`` so it is never
    mislabeled as a logout.
    """
    if provider == "buff":
        return _validate_buff_login(timeout=timeout)
    if provider == "yyyp":
        return _validate_youpin_login(timeout=timeout)
    if provider == "c5":
        return _validate_c5_login(timeout=timeout)
    if provider == "eco":
        return _validate_eco_login(timeout=timeout)
    return {
        "provider": provider,
        "ok": False,
        "indeterminate": False,
        "message": "该平台暂不支持自动校验",
    }


def _validation_result(
    provider: str,
    *,
    ok: bool,
    message: str,
    indeterminate: bool = False,
    account_name: str = "",
    user_id: Any = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "ok": bool(ok),
        "indeterminate": bool(indeterminate),
        "message": str(message),
        "account_name": str(account_name),
        "user_id": user_id,
    }


def _buff_sell_order_paintwear_params(
    window_low: float,
    window_high: float,
) -> tuple[str, str]:
    min_paintwear = f"{float(window_low):.9f}".rstrip("0").rstrip(".")
    # Collection ranges use an open right edge below 1.0. BUFF treats
    # max_paintwear as inclusive, so pull it back by one nanounit.
    buff_max_wear = (
        max(float(window_low), float(window_high) - 1e-9)
        if float(window_high) < 1.0
        else float(window_high)
    )
    max_paintwear = f"{buff_max_wear:.9f}".rstrip("0").rstrip(".")
    return min_paintwear, max_paintwear


def _runtime_error_to_validation(provider: str, exc: BaseException) -> dict[str, Any]:
    text = str(exc)
    name = provider_display_name(provider)
    if "登录已失效" in text or "请先登录" in text or "未登录" in text:
        return _validation_result(
            provider,
            ok=False,
            message=f"{name} 登录已失效，请重新登录",
        )
    if any(
        marker in text
        for marker in ("风控", "虚拟设备", "异常网络", "高频", "风险", "滑块", "访问校验")
    ):
        return _validation_result(provider, ok=False, message=text)
    if "429" in text or "频率过高" in text or "限流" in text:
        return _validation_result(
            provider,
            ok=False,
            indeterminate=True,
            message=f"{name} 正在限流，暂时无法确认",
        )
    if isinstance(exc, requests.RequestException):
        return _validation_result(
            provider,
            ok=False,
            indeterminate=True,
            message=f"{name} 校验请求失败：{text}",
        )
    return _validation_result(
        provider,
        ok=False,
        indeterminate=True,
        message=f"{name} 暂时无法确认：{text}",
    )


def _probe_youpin_collection_login(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    token = str(token or "").strip()
    match = re.match(r"^Bearer\s+(.+)$", token, flags=re.IGNORECASE)
    if match:
        token = match.group(1).strip()
    cookie = str(cookie or "").strip()
    if not token:
        return _validation_result(
            "yyyp",
            ok=False,
            message="APP 未获取悠悠有品登录凭证",
        )
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "AppType": "1",
        "Origin": "https://www.youpin898.com",
        "Referer": "https://www.youpin898.com/",
        "Authorization": f"Bearer {token}",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        response = requests.post(
            _YOUPIN_API,
            headers=headers,
            json={
                "templateId": str(_YOUPIN_LOGIN_CHECK_TEMPLATE_ID),
                "pageSize": 40,
                "pageIndex": 1,
                "sortType": 1,
                "listSortType": 1,
                "listType": 10,
                "stickersIsSort": False,
                "minAbrade": _LOGIN_CHECK_MIN_WEAR,
                "maxAbrade": _LOGIN_CHECK_MAX_WEAR,
            },
            timeout=timeout,
        )
        if response.status_code == 429:
            return _validation_result(
                "yyyp",
                ok=False,
                indeterminate=True,
                message="悠悠有品正在限流，暂时无法确认",
            )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return _runtime_error_to_validation("yyyp", exc)
    code = int(payload.get("Code") or payload.get("code") or 0)
    detail = str(payload.get("Msg") or payload.get("msg") or code)
    if code == 0:
        return _validation_result("yyyp", ok=True, message="悠悠有品登录有效")
    if _response_indicates_login_required(payload, detail):
        return _validation_result(
            "yyyp",
            ok=False,
            message=f"悠悠有品登录已失效：{detail}",
        )
    return _validation_result(
        "yyyp",
        ok=False,
        indeterminate=True,
        message=f"悠悠有品暂时无法确认：{detail or '未知响应'}",
    )


def _probe_c5_collection_login(
    cookie: str,
    token: str = "",
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    cookie = str(cookie or "").strip()
    token = str(token or "").strip()
    if not token and not _cookie_looks_authenticated(
        cookie, ("nc5_accesstoken", "c5token", "access_token", "ncaccess", "token")
    ):
        return _validation_result(
            "c5",
            ok=False,
            message="APP 未获取 C5GAME 登录凭证",
        )
    try:
        clear_c5_app_version()
    except Exception:
        pass
    errors: list[str] = []
    for fetcher in (_fetch_c5_via_search_api, _fetch_c5_via_napi):
        try:
            fetcher(
                ids=[_C5_LOGIN_CHECK_ITEM_ID],
                display_name="登录校验",
                min_wear=_LOGIN_CHECK_MIN_WEAR,
                max_wear=_LOGIN_CHECK_MAX_WEAR,
                max_pages=1,
                request_interval=0,
                cookie=cookie,
                token=token,
                progress=None,
                cancel_check=None,
            )
            return _validation_result("c5", ok=True, message="C5GAME 登录有效")
        except RuntimeError as exc:
            text = str(exc)
            errors.append(text)
            if "登录已失效" in text:
                return _validation_result(
                    "c5",
                    ok=False,
                    message="C5GAME 登录已失效，请重新登录",
                )
            if any(
                marker in text
                for marker in ("风控", "虚拟设备", "异常网络", "高频", "风险")
            ):
                return _validation_result("c5", ok=False, message=text)
            if "429" in text or "频率过高" in text:
                return _validation_result(
                    "c5",
                    ok=False,
                    indeterminate=True,
                    message="C5GAME 正在限流，暂时无法确认",
                )
            continue
        except requests.RequestException as exc:
            return _runtime_error_to_validation("c5", exc)
    prior = "；".join(errors) if errors else "网页采集接口不可用"
    return _validation_result(
        "c5",
        ok=False,
        indeterminate=True,
        message=f"C5GAME 暂时无法确认：{prior}",
    )


def _probe_eco_collection_login(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    if not token and not _cookie_looks_authenticated(
        cookie, ("token", "authorization", "eco_token", "access_token")
    ):
        return _validation_result(
            "eco",
            ok=False,
            message="APP 未获取 ECOSteam 登录凭证",
        )
    try:
        _post_eco_sell_query(
            goods_id=_ECO_LOGIN_CHECK_GOODS_ID,
            hash_name="",
            min_wear=_LOGIN_CHECK_MIN_WEAR,
            max_wear=_LOGIN_CHECK_MAX_WEAR,
            page=1,
            cookie=cookie,
            token=token,
        )
    except RuntimeError as exc:
        return _runtime_error_to_validation("eco", exc)
    except requests.RequestException as exc:
        return _runtime_error_to_validation("eco", exc)
    return _validation_result("eco", ok=True, message="ECOSteam 登录有效")


def _enrich_youpin_account_name(
    result: dict[str, Any],
    token: str,
    cookie: str,
    *,
    timeout: float,
    quick: bool,
) -> dict[str, Any]:
    if not result.get("ok") or quick:
        return result
    if result.get("account_name"):
        return result
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "AppType": "1",
        "Origin": "https://www.youpin898.com",
        "Referer": "https://www.youpin898.com/",
        "Authorization": f"Bearer {token}",
    }
    if cookie:
        headers["Cookie"] = cookie
    for url in _YOUPIN_USER_INFO_APIS[:1]:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue
        data = payload.get("Data") or payload.get("data") or {}
        if not isinstance(data, dict):
            continue
        nickname = str(
            data.get("NickName")
            or data.get("nickName")
            or data.get("Nickname")
            or data.get("nickname")
            or ""
        ).strip()
        user_id = (
            data.get("UserId")
            or data.get("userId")
            or data.get("Id")
            or data.get("id")
        )
        if nickname:
            enriched = dict(result)
            enriched["account_name"] = nickname
            enriched["user_id"] = user_id
            return enriched
    return result


def _lookup_c5_user_profile(
    cookie: str,
    token: str = "",
    *,
    timeout: float = 12.0,
) -> tuple[str, Any]:
    cookie = str(cookie or "").strip()
    token = _c5_effective_token(cookie, token)
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": _C5_ACCEPT_ENCODING,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.c5game.com",
        "Referer": "https://www.c5game.com/user-center/user",
    }
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["x-access-token"] = token
    for url in _C5_USER_CHECK_APIS[:1]:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                continue
            payload = response.json() if response.text else {}
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        return _extract_account_fields(payload)
    return "", None


def _lookup_eco_user_profile(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
) -> tuple[str, Any]:
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.ecosteam.cn",
        "Referer": "https://www.ecosteam.cn/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    for url in _ECO_USER_INFO_APIS[:1]:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                continue
            payload = response.json() if response.text else {}
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        body = _eco_result_payload(payload)
        nickname, user_id = _extract_account_fields(body)
        if not nickname and not user_id:
            nickname, user_id = _extract_account_fields(payload)
        if nickname or user_id:
            return nickname, user_id
    return "", None


def _enrich_c5_account_name(
    result: dict[str, Any],
    cookie: str,
    token: str,
    *,
    timeout: float,
    quick: bool,
) -> dict[str, Any]:
    if not result.get("ok") or quick:
        return result
    if result.get("account_name"):
        return result
    nickname, user_id = _lookup_c5_user_profile(cookie, token, timeout=timeout)
    if nickname:
        enriched = dict(result)
        enriched["account_name"] = nickname
        enriched["user_id"] = user_id
        return enriched
    return result


def _enrich_eco_account_name(
    result: dict[str, Any],
    token: str,
    cookie: str,
    *,
    timeout: float,
    quick: bool,
) -> dict[str, Any]:
    if not result.get("ok") or quick:
        return result
    if result.get("account_name"):
        return result
    nickname, user_id = _lookup_eco_user_profile(token, cookie, timeout=timeout)
    if nickname:
        enriched = dict(result)
        enriched["account_name"] = nickname
        enriched["user_id"] = user_id
        return enriched
    return result


def _validate_buff_login(*, timeout: float) -> dict[str, Any]:
    cookie = _buff_cookie()
    if "session=" not in cookie.lower():
        return _validation_result(
            "buff",
            ok=False,
            message="APP 未获取 BUFF 登录凭证",
        )
    min_paintwear, max_paintwear = _buff_sell_order_paintwear_params(
        _LOGIN_CHECK_MIN_WEAR,
        _LOGIN_CHECK_MAX_WEAR,
    )
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": (
            f"https://buff.163.com/goods/{_BUFF_LOGIN_CHECK_GOODS_ID}"
            "?from=market"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }
    try:
        response = requests.get(
            _BUFF_API,
            params={
                "game": "csgo",
                "goods_id": _BUFF_LOGIN_CHECK_GOODS_ID,
                "page_num": 1,
                "page_size": _BUFF_SELL_ORDER_PAGE_SIZE,
                "sort_by": _BUFF_SELL_ORDER_SORT_BY,
                "min_paintwear": min_paintwear,
                "max_paintwear": max_paintwear,
            },
            headers=headers,
            timeout=timeout,
        )
        if response.status_code == 429:
            return _validation_result(
                "buff",
                ok=False,
                indeterminate=True,
                message="BUFF 正在限流，暂时无法确认",
            )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return _validation_result(
            "buff",
            ok=False,
            indeterminate=True,
            message=f"BUFF 校验请求失败：{exc}",
        )
    code = str(payload.get("code") or "")
    detail = str(payload.get("error") or payload.get("msg") or code)
    if code == "OK":
        return _validation_result(
            "buff",
            ok=True,
            message="BUFF 登录有效",
        )
    if "login" in detail.lower() or "登录" in detail:
        return _validation_result(
            "buff",
            ok=False,
            message="BUFF 登录已失效，请重新登录",
        )
    return _validation_result(
        "buff",
        ok=False,
        indeterminate=True,
        message=f"BUFF 暂时无法确认：{detail or '未知响应'}",
    )


def _validate_youpin_login(*, timeout: float) -> dict[str, Any]:
    token, cookie = _youpin_auth()
    return validate_youpin_credentials(token, cookie, timeout=timeout)


def validate_youpin_credentials(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
    quick: bool = False,
) -> dict[str, Any]:
    """Validate Youpin credentials using the same listing API as collection."""
    token = str(token or "").strip()
    match = re.match(r"^Bearer\s+(.+)$", token, flags=re.IGNORECASE)
    if match:
        token = match.group(1).strip()
    cookie = str(cookie or "").strip()
    if not token:
        return _validation_result(
            "yyyp",
            ok=False,
            message="未捕获到悠悠有品 Token",
        )
    result = _probe_youpin_collection_login(token, cookie, timeout=timeout)
    return _enrich_youpin_account_name(
        result,
        token,
        cookie,
        timeout=timeout,
        quick=quick,
    )


def _extract_account_fields(data: Any) -> tuple[str, Any]:
    if not isinstance(data, dict):
        return "", None
    nested = data
    for key in ("data", "Data", "ResultData", "resultData"):
        child = data.get(key)
        if isinstance(child, dict):
            nested = child
            break
    nickname = str(
        nested.get("nickname")
        or nested.get("NickName")
        or nested.get("nickName")
        or nested.get("BuyerNickname")
        or nested.get("userName")
        or nested.get("UserName")
        or nested.get("name")
        or nested.get("Name")
        or ""
    ).strip()
    user_id = (
        nested.get("userId")
        or nested.get("UserId")
        or nested.get("uid")
        or nested.get("Uid")
        or nested.get("id")
        or nested.get("Id")
        or nested.get("openid")
        or nested.get("openId")
    )
    return nickname, user_id


def _response_indicates_login_required(payload: Any, text: str = "") -> bool:
    blob = f"{payload} {text}".lower()
    markers = (
        "未登录",
        "请登录",
        "not login",
        "not logged",
        "unauthorized",
        "login required",
        "token invalid",
        "token expired",
        "invalid token",
        "登录失效",
        "登录过期",
        "重新登录",
    )
    return any(marker in blob for marker in markers)


def validate_c5_credentials(
    cookie: str,
    token: str = "",
    *,
    timeout: float = 12.0,
    quick: bool = False,
) -> dict[str, Any]:
    """Validate C5 credentials using the same listing APIs as collection."""
    cookie = str(cookie or "").strip()
    token = _c5_effective_token(cookie, token)
    result = _probe_c5_collection_login(cookie, token, timeout=timeout)
    return _enrich_c5_account_name(
        result,
        cookie,
        token,
        timeout=timeout,
        quick=quick,
    )


def validate_c5_openapi_credentials(
    app_key: str,
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    app_key = str(app_key or "").strip()
    if len(app_key) < 16:
        return _validation_result(
            "c5",
            ok=False,
            message="未配置有效的 C5GAME 开放平台 app-key",
        )
    try:
        response = requests.get(
            _C5_OPENAPI_BALANCE,
            params={"app-key": app_key},
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Accept-Encoding": _C5_ACCEPT_ENCODING,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return _validation_result(
            "c5",
            ok=False,
            indeterminate=True,
            message=f"C5GAME 开放平台校验失败：{exc}",
        )
    try:
        payload = response.json() if response.text else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    error_code = str(payload.get("errorCode") or "")
    error_msg = str(payload.get("errorMsg") or "").strip()
    if error_code in {"400001", "401", "403"} or "app-key" in error_msg.lower() or "app-Key" in error_msg:
        return _validation_result(
            "c5",
            ok=False,
            message="C5GAME 开放平台 app-key 无效，请重新填写",
        )
    if error_code == "499103" or "白名单" in error_msg or "ip" in error_msg.lower():
        # Balance may not require whitelist; products/search does. Still surface IP hint.
        return _validation_result(
            "c5",
            ok=False,
            indeterminate=True,
            message=(
                "C5GAME 开放平台要求 IP 白名单："
                f"{error_msg or '请在个人中心-API管理中添加本机公网 IP'}"
            ),
        )
    if payload.get("success") is True or error_code in {"0", ""}:
        if response.status_code < 400:
            user_id = None
            data = payload.get("data")
            if isinstance(data, dict):
                user_id = data.get("userId") or data.get("user_id")
            return _validation_result(
                "c5",
                ok=True,
                message="C5GAME 开放平台 app-key 有效",
                account_name="OpenAPI",
                user_id=user_id,
            )
    if response.status_code >= 400:
        return _validation_result(
            "c5",
            ok=False,
            indeterminate=True,
            message=f"C5GAME 开放平台校验失败：HTTP {response.status_code}",
        )
    return _validation_result(
        "c5",
        ok=False,
        indeterminate=True,
        message=f"C5GAME 开放平台暂时无法确认：{error_msg or error_code or '未知错误'}",
    )


def _validate_c5_login(*, timeout: float) -> dict[str, Any]:
    cookie, token = _c5_auth()
    return validate_c5_credentials(cookie, token, timeout=timeout)


def _eco_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """ECO wraps business fields under StatusData on newer gateways."""
    for key in ("StatusData", "statusData", "Data", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def validate_eco_credentials(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
    quick: bool = False,
) -> dict[str, Any]:
    """Validate ECO credentials using the same SellGoodsQuery API as collection."""
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    result = _probe_eco_collection_login(token, cookie, timeout=timeout)
    return _enrich_eco_account_name(
        result,
        token,
        cookie,
        timeout=timeout,
        quick=quick,
    )


def _validate_eco_login(*, timeout: float) -> dict[str, Any]:
    token, cookie = _eco_auth()
    return validate_eco_credentials(token, cookie, timeout=timeout)


def _overlapping_ids(
    mapping: dict,
    min_wear: float,
    max_wear: float,
) -> list[int]:
    result: list[int] = []
    for appearance, raw_id in mapping.items():
        bounds = _WEAR_BOUNDS.get(str(appearance))
        if bounds is None or bounds[1] <= min_wear or bounds[0] >= max_wear:
            continue
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in result:
            result.append(value)
    return result


def _coerce_positive_ids(*values: Any) -> list[int]:
    result: list[int] = []
    for raw in values:
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                result.extend(_coerce_positive_ids(item))
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in result:
            result.append(value)
    return result


def _first_number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = item.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _nested_dict(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _extract_listing_wear(item: dict[str, Any]) -> float | None:
    asset = _nested_dict(item, "assetInfo", "AssetInfo", "asset_info", "Asset")
    wear = _first_number(
        item,
        "wear",
        "Wear",
        "floatWear",
        "FloatWear",
        "paintWear",
        "PaintWear",
        "Abrade",
        "abrade",
        "WearValue",
        "wearValue",
    )
    if wear is None:
        wear = _first_number(
            asset,
            "wear",
            "Wear",
            "floatWear",
            "FloatWear",
            "paintWear",
            "PaintWear",
            "Abrade",
            "abrade",
        )
    return wear


def _extract_listing_price(item: dict[str, Any]) -> float | None:
    return _first_number(
        item,
        "price",
        "Price",
        "cnyPrice",
        "CnyPrice",
        "sellerPrice",
        "SellerPrice",
        "SellingPrice",
        "sellingPrice",
        "Amount",
        "amount",
    )


def _extract_listing_id(item: dict[str, Any]) -> str:
    for key in (
        "id",
        "Id",
        "ID",
        "productId",
        "ProductId",
        "GoodsNum",
        "GoodsNumber",
        "goodsNum",
        "goodsNumber",
        "CommodityNo",
        "listing_id",
    ):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _iter_listing_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "list",
        "List",
        "items",
        "Items",
        "PageResult",
        "pageResult",
        "records",
        "Records",
        "rows",
        "Rows",
        "CommodityList",
        "products",
        "Products",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("data", "Data", "StatusData", "statusData", "ResultData", "resultData"):
        nested = payload.get(key)
        rows = _iter_listing_rows(nested)
        if rows:
            return rows
    return []


def _c5_request_headers(
    *,
    item_id: int | str,
    cookie: str,
    token: str,
    min_wear: float,
    max_wear: float,
    timestamp_ms: str | None = None,
    device_id: str | None = None,
    signature: str = "",
) -> dict[str, str]:
    referer = (
        f"https://www.c5game.com/csgo/{item_id}/item/sell"
        f"?minWear={min_wear:.8f}&maxWear={max_wear:.8f}"
    )
    client = _c5_client_headers()
    access_token = _c5_effective_token(cookie, token)
    now_ms = str(timestamp_ms or int(time.time() * 1000))
    current_device_id = (
        client.get("x-device-id") if device_id is None else str(device_id or "")
    )
    traffic_tag = _cookie_value(cookie, "x-traffic-tag") or now_ms
    headers = {
        "User-Agent": client.get("User-Agent") or _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": _C5_ACCEPT_ENCODING,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.c5game.com",
        "Referer": referer,
        # Current website client markers. Never send App-Version: stale values return 102.
        "x-app-channel": client.get("x-app-channel") or "WEB",
        "x-start-req-time": now_ms,
        "x-source": client.get("x-source") or "1",
        "x-area": client.get("x-area") or "1",
        "x-sign": str(signature or ""),
        "x-device-id": current_device_id or "",
        "x-traffic-tag": client.get("x-traffic-tag") or traffic_tag,
        "x-device-os": client.get("x-device-os") or "Windows NT 10.0; Win64; x64",
        "x-device-model": client.get("x-device-model") or "Chrome 124.0.0.0",
    }
    if cookie:
        headers["Cookie"] = cookie
    headers["x-access-token"] = access_token
    return headers


def clear_c5_app_version() -> None:
    """Drop persisted App-Version so HTTP collection stops sending a rejected value."""
    path = _app_auth_path(_C5_CLIENT_HEADERS_FILE)
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    if "App-Version" not in data and "app-version" not in data:
        return
    data.pop("App-Version", None)
    data.pop("app-version", None)
    data["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_auth_json(_C5_CLIENT_HEADERS_FILE, data)


def _c5_napi_headers(*, item_id: int | str, cookie: str, token: str) -> dict[str, str]:
    """Minimal headers used by public C5 website scrapers (platform=2)."""
    client = _c5_client_headers()
    headers = {
        "User-Agent": client.get("User-Agent") or _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": _C5_ACCEPT_ENCODING,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.c5game.com",
        "Referer": f"https://www.c5game.com/csgo/{item_id}/item/sell",
        "platform": "2",
    }
    if cookie:
        headers["Cookie"] = cookie
    access_token = _c5_effective_token(cookie, token)
    if access_token:
        headers["x-access-token"] = access_token
    return headers


def _eco_request_headers(*, goods_id: int | str, cookie: str, token: str) -> dict[str, str]:
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.ecosteam.cn",
        "Referer": f"https://www.ecosteam.cn/goods/730-{goods_id}-1-laypagesale-0-1.html",
    }
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
        # ECO pages also persist loginToken in cookies; mirror as header fallbacks.
        if "loginToken=" not in cookie.lower():
            headers["loginToken"] = token
    return headers


def _merge_platform_ids(
    mapping: dict,
    min_wear: float,
    max_wear: float,
    extra_ids: list[int] | None = None,
) -> list[int]:
    ids = _overlapping_ids(mapping, min_wear, max_wear)
    for value in _coerce_positive_ids(extra_ids or []):
        if value not in ids:
            ids.append(value)
    return ids


def _overlapping_steam_targets(
    template: SkinTemplate,
    min_wear: float,
    max_wear: float,
) -> list[tuple[str, int]]:
    """Return ``(steam_hash_name, eco_goods_id)`` pairs for exteriors in range.

    ECO's current ``SellGoodsQuery`` accepts HashName and rejects many legacy
    GoodsId values with「商品不存在」, so HashName is the primary query key.
    eco_goods_id is kept only for purchase-page deep links / browser fallback.
    """
    steam = getattr(template, "steam", None) or {}
    eco = getattr(template, "eco", None) or {}
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for appearance, raw_hash in steam.items():
        bounds = _WEAR_BOUNDS.get(str(appearance))
        if bounds is None or bounds[1] <= min_wear or bounds[0] >= max_wear:
            continue
        hash_name = str(raw_hash or "").strip()
        if not hash_name or hash_name in seen:
            continue
        seen.add(hash_name)
        goods_id = 0
        try:
            goods_id = int(eco.get(appearance) or 0)
        except (TypeError, ValueError):
            goods_id = 0
        out.append((hash_name, goods_id if goods_id > 0 else 0))
    return out


def _in_range(value: float, low: float, high: float) -> bool:
    return low <= value <= high if high >= 1.0 else low <= value < high


def _split_wear_windows(
    min_wear: float,
    max_wear: float,
) -> list[tuple[float, float]]:
    """Split a wear range on exterior buckets (FN/MW/FT/WW/BS).

    Example: 0.11–0.27 → (0.11, 0.15) 略磨 + (0.15, 0.27) 久经.
    Marketplace upper bounds are inclusive, while our collection ranges use an
    open right edge below 1.0.  The caller still validates every returned wear
    against its individual window, so exact boundary listings are kept once in
    the following window.
    """

    low = Decimal(str(min_wear))
    high = Decimal(str(max_wear))
    if high <= low:
        return []
    # Interior cuts are exterior bucket edges (exclude 0.0 / 1.0 endpoints).
    exterior_cuts = sorted(
        {
            Decimal(str(bounds[1]))
            for bounds in _WEAR_BOUNDS.values()
            if 0.0 < float(bounds[1]) < 1.0
        }
    )
    boundaries = [low]
    boundaries.extend(cut for cut in exterior_cuts if low < cut < high)
    boundaries.append(high)
    return [
        (float(start), float(end))
        for start, end in zip(boundaries, boundaries[1:])
        if end > start
    ]


def fetch_buff_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 0,
    request_interval: float = 2.0,
    progress: Callable[[str], None] | None = None,
    extra_ids: list[int] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    wear_windows = _split_wear_windows(min_wear, max_wear)
    if not wear_windows:
        return []
    cookie = _buff_cookie()
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://buff.163.com/market/csgo",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    out: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    request_no = 0
    page_limit = _effective_page_limit(max_pages)
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )

    def collect_window(
        goods_id: int,
        window_low: float,
        window_high: float,
        *,
        window_kept: list[int],
    ) -> None:
        nonlocal request_no
        min_paintwear, max_paintwear = _buff_sell_order_paintwear_params(
            window_low,
            window_high,
        )
        for page in range(1, page_limit + 1):
            raise_if_cancelled(cancel_check)
            if _collection_window_row_limit_reached(window_kept[0]):
                break
            if request_no:
                interruptible_wait(max(1.0, request_interval), cancel_check)
            request_no += 1
            if progress:
                progress(
                    "BUFF · "
                    f"{display_name} · 磨损 {window_low:g}–{window_high:g} · "
                    f"第 {page} 页"
                )
            response = requests.get(
                _BUFF_API,
                params={
                    "game": "csgo",
                    "goods_id": goods_id,
                    "page_num": page,
                    "page_size": _BUFF_SELL_ORDER_PAGE_SIZE,
                    "sort_by": _BUFF_SELL_ORDER_SORT_BY,
                    "min_paintwear": min_paintwear,
                    "max_paintwear": max_paintwear,
                },
                headers=headers,
                timeout=18,
            )
            raise_if_cancelled(cancel_check)
            if response.status_code == 429:
                raise RuntimeError("BUFF 返回访问频率过高，已立即停止本平台采集")
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "OK":
                detail = payload.get("error") or payload.get("code") or "未知错误"
                raise RuntimeError(f"BUFF 在售接口失败：{detail}")
            data = payload.get("data") or {}
            items = data.get("items") or []
            if not items:
                break
            page_hit_cap = False
            page_kept = 0
            for item in items:
                if _collection_window_row_limit_reached(window_kept[0]):
                    break
                raw_wear = (item.get("asset_info") or {}).get("paintwear")
                try:
                    wear = float(raw_wear)
                    price = float(item.get("price"))
                except (TypeError, ValueError):
                    continue
                if (
                    price <= 0
                    or not _in_range(wear, min_wear, max_wear)
                    or not _in_range(wear, window_low, window_high)
                ):
                    continue
                if price_cap is not None and price > price_cap:
                    page_hit_cap = True
                    continue
                order_id = str(item.get("id") or "")
                dedupe_key = order_id or f"{goods_id}:{wear}:{price}"
                if dedupe_key in seen_listing_ids:
                    continue
                seen_listing_ids.add(dedupe_key)
                page_kept += 1
                window_kept[0] += 1
                out.append(
                    {
                        "goods_name": display_name,
                        "float_value": wear,
                        "price": price,
                        "goods_id": f"buff:{order_id or goods_id}:{wear}",
                        "platform": "buff",
                        "listing_id": order_id,
                        "purchase_link": (
                            f"https://buff.163.com/goods/{goods_id}"
                            f"?from=market#tab=selling"
                        ),
                    }
                )
            # Price-asc pages: once the page is entirely above the recipe cap, stop.
            if price_cap is not None and page_hit_cap and page_kept == 0:
                if progress:
                    progress(
                        f"BUFF · {display_name} · 已超过配方单价 2 倍，停止本窗翻页"
                    )
                break
            if _collection_window_row_limit_reached(window_kept[0]):
                if progress:
                    progress(
                        f"BUFF · {display_name} · 本磨损区间已满 "
                        f"{_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW} 条，停止本窗"
                    )
                break
            try:
                total_page = int(data.get("total_page") or 0)
            except (TypeError, ValueError):
                total_page = 0
            if total_page > 0 and page >= total_page:
                break

    for window_low, window_high in wear_windows:
        raise_if_cancelled(cancel_check)
        window_kept = [0]
        ids = _merge_platform_ids(
            template.buff,
            window_low,
            window_high,
            extra_ids,
        )
        for goods_id in ids:
            if _collection_window_row_limit_reached(window_kept[0]):
                break
            collect_window(
                goods_id,
                window_low,
                window_high,
                window_kept=window_kept,
            )
    return out


def fetch_youpin_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 0,
    request_interval: float = 2.0,
    progress: Callable[[str], None] | None = None,
    extra_ids: list[int] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    wear_windows = _split_wear_windows(min_wear, max_wear)
    if not wear_windows:
        return []
    token, cookie = _youpin_auth()
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "AppType": "1",
        "Origin": "https://www.youpin898.com",
        "Referer": "https://www.youpin898.com/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    out: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    request_no = 0
    page_size = 40
    page_limit = _effective_page_limit(max_pages)
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )

    def collect_window(
        template_id: int,
        window_low: float,
        window_high: float,
        *,
        window_kept: list[int],
    ) -> None:
        nonlocal request_no
        for page in range(1, page_limit + 1):
            raise_if_cancelled(cancel_check)
            if _collection_window_row_limit_reached(window_kept[0]):
                break
            if request_no:
                interruptible_wait(max(1.0, request_interval), cancel_check)
            request_no += 1
            if progress:
                progress(
                    "悠悠有品 · "
                    f"{display_name} · 磨损 {window_low:g}–{window_high:g} · "
                    f"第 {page} 页"
                )
            response = requests.post(
                _YOUPIN_API,
                headers=headers,
                json={
                    "templateId": str(template_id),
                    "pageSize": page_size,
                    "pageIndex": page,
                    "sortType": 1,
                    "listSortType": 1,
                    "listType": 10,
                    "stickersIsSort": False,
                    "minAbrade": window_low,
                    "maxAbrade": window_high,
                },
                timeout=18,
            )
            raise_if_cancelled(cancel_check)
            if response.status_code == 429:
                raise RuntimeError("悠悠有品返回访问频率过高，已立即停止本平台采集")
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("Code") or 0) != 0:
                raise RuntimeError(
                    "悠悠有品在售接口失败："
                    f"{payload.get('Msg') or payload.get('Code')}"
                )
            items = (payload.get("Data") or {}).get("CommodityList") or []
            if not items:
                break
            page_hit_cap = False
            page_kept = 0
            for item in items:
                if _collection_window_row_limit_reached(window_kept[0]):
                    break
                wear = _extract_listing_wear(item)
                price = _extract_listing_price(item)
                if wear is None or price is None:
                    continue
                if (
                    price <= 0
                    or not _in_range(wear, min_wear, max_wear)
                    or not _in_range(wear, window_low, window_high)
                ):
                    continue
                if price_cap is not None and price > price_cap:
                    page_hit_cap = True
                    continue
                listing_id = str(item.get("Id") or item.get("CommodityNo") or "")
                dedupe_key = listing_id or f"{template_id}:{wear}:{price}"
                if dedupe_key in seen_listing_ids:
                    continue
                seen_listing_ids.add(dedupe_key)
                page_kept += 1
                window_kept[0] += 1
                out.append(
                    {
                        "goods_name": display_name,
                        "float_value": wear,
                        "price": price,
                        "goods_id": f"yyyp:{listing_id}:{wear}",
                        "platform": "yyyp",
                        "listing_id": listing_id,
                        "purchase_link": (
                            "https://www.youpin898.com/market/goods-list"
                            f"?listType=10&templateId={quote(str(template_id))}"
                            "&gameId=730"
                        ),
                    }
                )
            # Price-asc pages: once the page is entirely above the recipe cap, stop.
            # Do not stop merely because page_kept==0 (wear/parse filters); that
            # incorrectly aborts when the first page is all out-of-window.
            if price_cap is not None and page_hit_cap and page_kept == 0:
                if progress:
                    progress(
                        f"悠悠有品 · {display_name} · 已超过配方单价 2 倍，停止本窗翻页"
                    )
                break
            if _collection_window_row_limit_reached(window_kept[0]):
                if progress:
                    progress(
                        f"悠悠有品 · {display_name} · 本磨损区间已满 "
                        f"{_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW} 条，停止本窗"
                    )
                break
            if len(items) < page_size:
                break

    for window_index, (window_low, window_high) in enumerate(wear_windows):
        raise_if_cancelled(cancel_check)
        if window_index > 0:
            if progress:
                progress(
                    f"悠悠有品 · {display_name} · 换磨损区间前等待 "
                    f"{max(1.0, request_interval):g} 秒"
                )
            interruptible_wait(max(1.0, request_interval), cancel_check)
        window_kept = [0]
        ids = _merge_platform_ids(
            template.yyyp,
            window_low,
            window_high,
            extra_ids,
        )
        for template_id in ids:
            if _collection_window_row_limit_reached(window_kept[0]):
                break
            collect_window(
                template_id,
                window_low,
                window_high,
                window_kept=window_kept,
            )
    return out


def _raise_c5_openapi_error(payload: dict[str, Any]) -> None:
    error_code = str(payload.get("errorCode") or payload.get("code") or "")
    error_msg = str(payload.get("errorMsg") or payload.get("msg") or "").strip()
    if error_code in {"400001", "401", "403"}:
        raise RuntimeError("C5GAME 开放平台 app-key 无效，请重新填写后再采集")
    if error_code == "499103" or "白名单" in error_msg:
        raise RuntimeError(
            "C5GAME 开放平台拒绝了当前 IP："
            f"{error_msg or '请在个人中心-API管理中把本机公网 IP 加入白名单'}"
            "（在售搜索接口必须配置白名单）"
        )
    if payload.get("success") is False and error_code not in {"", "0"}:
        raise RuntimeError(f"C5GAME 开放平台在售接口失败：{error_msg or error_code}")


def _rows_from_c5_openapi_payload(
    *,
    payload: dict[str, Any],
    item_id: int,
    display_name: str,
    min_wear: float,
    max_wear: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Parse OpenAPI products/search response; return (rows, has_more)."""
    _raise_c5_openapi_error(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    has_more = bool(data.get("hasMore") or data.get("has_more"))
    out: list[dict[str, Any]] = []
    for item in _iter_listing_rows(data):
        wear = _extract_listing_wear(item)
        price = _extract_listing_price(item)
        if wear is None or price is None or price <= 0:
            continue
        if not _in_range(wear, min_wear, max_wear):
            continue
        listing_id = _extract_listing_id(item)
        out.append(
            {
                "goods_name": display_name,
                "float_value": wear,
                "price": price,
                "goods_id": f"c5:{listing_id or item_id}:{wear}",
                "platform": "c5",
                "listing_id": listing_id,
                "purchase_link": (
                    f"https://www.c5game.com/csgo/{item_id}/item/sell"
                    f"?minWear={min_wear:.8f}&maxWear={max_wear:.8f}"
                ),
            }
        )
    if not out and not has_more and not _iter_listing_rows(data):
        has_more = False
    return out, has_more


def _rows_from_c5_payload(
    *,
    payload: dict[str, Any],
    item_id: int,
    display_name: str,
    min_wear: float,
    max_wear: float,
) -> list[dict[str, Any]]:
    error_code = str(payload.get("errorCode") or payload.get("code") or "")
    error_msg = str(payload.get("errorMsg") or payload.get("msg") or "")
    risk_rejected = any(
        marker in error_msg for marker in ("虚拟设备", "异常网络", "高频", "风险")
    )
    if risk_rejected:
        raise C5AccessGateError(
            f"C5GAME 风控拒绝了当前采集环境：{error_msg}",
            needs_verify=True,
        )
    if error_code in {"101", "401", "403"} or _response_indicates_login_required(
        payload, ""
    ):
        raise RuntimeError("C5GAME 登录已失效，请重新登录后再采集")
    if error_code == "102":
        try:
            clear_c5_app_version()
        except Exception:
            pass
        raise RuntimeError(
            "C5GAME 拒绝了客户端版本头，暂时无法抓取精确磨损挂单；请稍后再试或改用打开链接"
        )
    if payload.get("success") is False and error_code not in {"", "0"}:
        raise RuntimeError(f"C5GAME 在售接口失败：{error_msg or error_code}")
    out: list[dict[str, Any]] = []
    for item in _iter_listing_rows(payload):
        wear = _extract_listing_wear(item)
        price = _extract_listing_price(item)
        if wear is None or price is None or price <= 0:
            continue
        if not _in_range(wear, min_wear, max_wear):
            continue
        listing_id = _extract_listing_id(item)
        out.append(
            {
                "goods_name": display_name,
                "float_value": wear,
                "price": price,
                "goods_id": f"c5:{listing_id or item_id}:{wear}",
                "platform": "c5",
                "listing_id": listing_id,
                "purchase_link": (
                    f"https://www.c5game.com/csgo/{item_id}/item/sell"
                    f"?minWear={min_wear:.8f}&maxWear={max_wear:.8f}"
                ),
            }
        )
    return out


def _rows_from_eco_payload(
    *,
    payload: dict[str, Any],
    goods_id: int,
    display_name: str,
    min_wear: float,
    max_wear: float,
) -> list[dict[str, Any]]:
    body_payload = _eco_result_payload(payload)
    result_code = str(
        body_payload.get("ResultCode")
        or body_payload.get("resultCode")
        or payload.get("ResultCode")
        or ""
    )
    result_msg = str(
        body_payload.get("ResultMsg")
        or body_payload.get("resultMsg")
        or payload.get("StatusMsg")
        or ""
    )
    if result_code in {"4001", "401", "403"} or "未登录" in result_msg:
        raise RuntimeError("ECOSteam 登录已失效，请重新登录后再采集")
    if result_code == "429":
        needs_slider = _eco_message_has_slider_signal(result_msg)
        raise EcoAccessGateError(
            "ECOSteam 触发了访问校验（429），请稍后重试或完成滑块验证"
            + (f"：{result_msg}" if result_msg else ""),
            result_msg=result_msg,
            needs_slider=needs_slider,
        )
    if "商品不存在" in result_msg or result_code in {"404", "4004"}:
        raise RuntimeError(f"ECOSteam 商品不存在（GoodsId={goods_id}）")
    if result_code not in {"", "0", "200", "OK", "ok"}:
        raise RuntimeError(f"ECOSteam 在售接口失败：{result_msg or result_code}")
    result_data = body_payload.get("ResultData")
    if not isinstance(result_data, dict):
        result_data = body_payload
    out: list[dict[str, Any]] = []
    for item in _iter_listing_rows(result_data):
        wear = _extract_listing_wear(item)
        price = _extract_listing_price(item)
        if wear is None or price is None or price <= 0:
            continue
        if not _in_range(wear, min_wear, max_wear):
            continue
        listing_id = _extract_listing_id(item)
        link_id = int(goods_id or 0)
        purchase_link = (
            f"https://www.ecosteam.cn/goods/730-{link_id}-1-laypagesale-0-1"
            if link_id > 0
            else "https://www.ecosteam.cn/market/730-1.html?game=730"
        )
        out.append(
            {
                "goods_name": display_name,
                "float_value": wear,
                "price": price,
                "goods_id": f"eco:{listing_id or link_id or 'hash'}:{wear}",
                "platform": "eco",
                "listing_id": listing_id,
                "purchase_link": purchase_link,
            }
        )
    return out


class _EcoSalePageParser(HTMLParser):
    """Extract server-rendered sale rows from the current ECO goods page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._in_sale_body = False
        self._row: dict[str, str] | None = None
        self._wear_text: list[str] | None = None
        self._price_text: list[str] | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = self._attrs(attrs)
        classes = set(values.get("class", "").split())
        if tag == "tbody" and values.get("data-saletradetype") == "1":
            self._in_sale_body = True
            return
        if not self._in_sale_body:
            return
        if tag == "tr":
            self._row = {}
            return
        if self._row is None:
            return
        if tag == "div" and "img-wrap" in classes:
            self._row["listing_id"] = values.get("data-goodsnumber", "")
            self._row["asset_id"] = values.get("data-assetid", "")
            self._row["stock_id"] = values.get("data-stockid", "")
        elif tag == "p" and "WearRate" in classes:
            self._wear_text = []
        elif tag == "span" and "price" in classes:
            self._price_text = []

    def handle_data(self, data: str) -> None:
        if self._wear_text is not None:
            self._wear_text.append(data)
        if self._price_text is not None:
            self._price_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_sale_body:
            return
        if tag == "span" and self._price_text is not None:
            if self._row is not None:
                self._row["price"] = "".join(self._price_text).strip()
            self._price_text = None
        elif tag == "p" and self._wear_text is not None:
            if self._row is not None:
                self._row["wear"] = "".join(self._wear_text).strip()
            self._wear_text = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._wear_text = None
            self._price_text = None
        elif tag == "tbody":
            self._in_sale_body = False


def _rows_from_eco_html(
    *,
    html: str,
    goods_id: int,
    display_name: str,
    min_wear: float,
    max_wear: float,
) -> list[dict[str, Any]]:
    parser = _EcoSalePageParser()
    parser.feed(str(html or ""))
    purchase_link = (
        f"https://www.ecosteam.cn/goods/730-{goods_id}"
        "-1-laypagesale-0-1.html"
    )
    out: list[dict[str, Any]] = []
    for item in parser.rows:
        wear_match = re.search(r"(?:0|1)\.\d+", item.get("wear", ""))
        price_match = re.search(r"\d+(?:\.\d+)?", item.get("price", ""))
        if wear_match is None or price_match is None:
            continue
        wear = float(wear_match.group(0))
        price = float(price_match.group(0))
        if price <= 0 or not _in_range(wear, min_wear, max_wear):
            continue
        listing_id = item.get("listing_id") or item.get("asset_id") or ""
        out.append(
            {
                "goods_name": display_name,
                "float_value": wear,
                "price": price,
                "goods_id": f"eco:{listing_id or goods_id}:{wear}",
                "platform": "eco",
                "listing_id": listing_id,
                "purchase_link": purchase_link,
            }
        )
    return out


def _fetch_c5_candidates_openapi(
    *,
    ids: list[int],
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int,
    request_interval: float,
    app_key: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
) -> list[dict[str, Any]]:
    """Official OpenAPI path — avoids website App-Version / console-ban gates."""
    out: list[dict[str, Any]] = []
    request_no = 0
    # Doc: products/search qps = 1/s.
    interval = max(1.0, float(request_interval))
    for item_id in ids:
        page = 1
        while page <= _effective_page_limit(max_pages):
            raise_if_cancelled(cancel_check)
            if request_no:
                interruptible_wait(interval, cancel_check)
            request_no += 1
            if progress:
                progress(f"C5GAME · OpenAPI · {display_name} · 第 {page} 页")
            body = {
                "itemId": int(item_id),
                "appId": 730,
                "wearMin": float(min_wear),
                "wearMax": float(max_wear),
                "pageSize": 50,
                "pageNum": page,
                "assetType": 1,
            }
            response = requests.post(
                _C5_OPENAPI_PRODUCTS_SEARCH,
                params={"app-key": app_key},
                json=body,
                headers={
                    "User-Agent": _UA,
                    "Accept": "application/json",
                    "Accept-Encoding": _C5_ACCEPT_ENCODING,
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
            raise_if_cancelled(cancel_check)
            if response.status_code == 429:
                raise RuntimeError("C5GAME 开放平台返回访问频率过高，已立即停止本平台采集")
            try:
                payload = response.json() if response.text else {}
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                raise RuntimeError("C5GAME 开放平台在售接口返回异常")
            if response.status_code >= 400 and payload.get("success") is not False:
                raise RuntimeError(
                    f"C5GAME 开放平台在售接口失败：HTTP {response.status_code}"
                )
            rows, has_more = _rows_from_c5_openapi_payload(
                payload=payload,
                item_id=item_id,
                display_name=display_name,
                min_wear=min_wear,
                max_wear=max_wear,
            )
            out.extend(rows)
            if not has_more and not rows:
                break
            if not has_more:
                break
            page += 1
    return out


def _c5_response_is_html(response: requests.Response) -> bool:
    ctype = str(response.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        return True
    text = (response.text or "")[:200].lower()
    return "<!doctype html" in text or "<html" in text


def _fetch_c5_via_napi(
    *,
    ids: list[int],
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int,
    request_interval: float,
    cookie: str,
    token: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    """Website napi sell list (platform=2). Filter wear locally when API ignores range."""
    out: list[dict[str, Any]] = []
    request_no = 0
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )
    for item_id in ids:
        headers = _c5_napi_headers(item_id=item_id, cookie=cookie, token=token)
        for page in range(1, _effective_page_limit(max_pages) + 1):
            raise_if_cancelled(cancel_check)
            if _collection_window_row_limit_reached(len(out)):
                break
            if request_no:
                interruptible_wait(max(1.0, request_interval), cancel_check)
            request_no += 1
            if progress:
                progress(f"C5GAME · {display_name} · 第 {page} 页")
            response = requests.get(
                _C5_NAPI_SELL_LIST_API,
                params={
                    "itemId": item_id,
                    "page": page,
                    "limit": _C5_SELL_LIST_PAGE_SIZE,
                    "orderBy": _C5_SELL_LIST_ORDER_BY_PRICE_ASC,
                    "minWear": f"{min_wear:.8f}",
                    "maxWear": f"{max_wear:.8f}",
                },
                headers=headers,
                timeout=18,
            )
            raise_if_cancelled(cancel_check)
            if response.status_code == 429:
                raise RuntimeError("C5GAME 返回访问频率过高，已立即停止本平台采集")
            # HTML here usually means the napi route bounced to the SPA, not that
            # saved cookies are dead — fall through to other collectors.
            if _c5_response_is_html(response):
                raise RuntimeError("C5GAME 网页 napi 不可用，改用其它接口")
            if response.status_code == 404:
                break
            response.raise_for_status()
            try:
                payload = response.json() if response.text else {}
            except ValueError as exc:
                raise RuntimeError("C5GAME 在售接口返回异常") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("C5GAME 在售接口返回异常")
            rows = _rows_from_c5_payload(
                payload=payload,
                item_id=item_id,
                display_name=display_name,
                min_wear=min_wear,
                max_wear=max_wear,
            )
            if price_cap is not None:
                kept = [
                    row
                    for row in rows
                    if float(row.get("price") or 0) <= price_cap
                ]
                hit_cap = len(kept) < len(rows)
                room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                out.extend(kept[:room])
                if hit_cap and not kept:
                    break
            else:
                room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                out.extend(rows[:room])
            if _collection_window_row_limit_reached(len(out)):
                break
            if not _iter_listing_rows(payload):
                break
        if _collection_window_row_limit_reached(len(out)):
            break
    return out


_C5_SIGNER_TLS = threading.local()


@contextmanager
def c5_signer_collection_scope() -> Iterator[None]:
    """Reuse one ``C5WebSigner`` for all search-api calls in the current thread."""
    if getattr(_C5_SIGNER_TLS, "active", False):
        yield
        return
    _C5_SIGNER_TLS.active = True
    _C5_SIGNER_TLS.signer = None
    try:
        yield
    finally:
        signer = getattr(_C5_SIGNER_TLS, "signer", None)
        _C5_SIGNER_TLS.signer = None
        _C5_SIGNER_TLS.active = False
        if signer is not None:
            signer.close()


def _acquire_c5_signer(device_id: str) -> tuple[Any | None, bool]:
    """Return ``(signer, owns_lifecycle)`` for one search-api collection batch."""
    if not device_id:
        return None, False
    if getattr(_C5_SIGNER_TLS, "active", False):
        signer = getattr(_C5_SIGNER_TLS, "signer", None)
        if signer is None:
            from core.c5_web_signer import C5WebSigner

            signer = C5WebSigner()
            try:
                signer.__enter__()
            except Exception:
                signer.close()
                raise
            _C5_SIGNER_TLS.signer = signer
        return signer, False
    from core.c5_web_signer import C5WebSigner

    signer = C5WebSigner()
    try:
        signer.__enter__()
    except Exception:
        signer.close()
        raise
    return signer, True


def _fetch_c5_via_search_api(
    *,
    ids: list[int],
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int,
    request_interval: float,
    cookie: str,
    token: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    request_no = 0
    client = _c5_client_headers()
    device_id = str(client.get("x-device-id") or "").strip()
    access_token = _c5_effective_token(cookie, token)
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )
    signer, owns_signer = _acquire_c5_signer(device_id)
    try:
        for item_id in ids:
            pathname = f"/search/v2/sell/{item_id}/list"
            for page in range(1, _effective_page_limit(max_pages) + 1):
                raise_if_cancelled(cancel_check)
                if _collection_window_row_limit_reached(len(out)):
                    break
                if request_no:
                    interruptible_wait(max(1.0, request_interval), cancel_check)
                timestamp_ms = str(int(time.time() * 1000))
                signature = (
                    signer.sign(pathname, "GET", timestamp_ms, access_token)
                    if signer is not None
                    else ""
                )
                headers = _c5_request_headers(
                    item_id=item_id,
                    cookie=cookie,
                    token=token,
                    min_wear=min_wear,
                    max_wear=max_wear,
                    timestamp_ms=timestamp_ms,
                    device_id=device_id,
                    signature=signature,
                )
                request_no += 1
                if progress:
                    progress(f"C5GAME · {display_name} · 第 {page} 页")
                response = requests.get(
                    _C5_SELL_LIST_API.format(item_id=item_id),
                    params={
                        "itemId": item_id,
                        "page": page,
                        "limit": _C5_SELL_LIST_PAGE_SIZE,
                        "orderBy": _C5_SELL_LIST_ORDER_BY_PRICE_ASC,
                        "minWear": f"{min_wear:.8f}",
                        "maxWear": f"{max_wear:.8f}",
                    },
                    headers=headers,
                    timeout=18,
                )
                raise_if_cancelled(cancel_check)
                if response.status_code == 429:
                    raise RuntimeError("C5GAME 返回访问频率过高，已立即停止本平台采集")
                try:
                    payload = response.json() if response.text else {}
                except ValueError as exc:
                    response.raise_for_status()
                    raise RuntimeError("C5GAME 在售接口返回异常") from exc
                if not isinstance(payload, dict):
                    raise RuntimeError("C5GAME 在售接口返回异常")
                # Parse C5's JSON error before raise_for_status so risk-control
                # responses are not misreported as an expired login.
                if response.status_code >= 400:
                    _rows_from_c5_payload(
                        payload=payload,
                        item_id=item_id,
                        display_name=display_name,
                        min_wear=min_wear,
                        max_wear=max_wear,
                    )
                    response.raise_for_status()
                rows = _rows_from_c5_payload(
                    payload=payload,
                    item_id=item_id,
                    display_name=display_name,
                    min_wear=min_wear,
                    max_wear=max_wear,
                )
                if price_cap is not None:
                    kept = [
                        row
                        for row in rows
                        if float(row.get("price") or 0) <= price_cap
                    ]
                    hit_cap = len(kept) < len(rows)
                    room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                    out.extend(kept[:room])
                    if hit_cap and not kept:
                        break
                else:
                    room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                    out.extend(rows[:room])
                if _collection_window_row_limit_reached(len(out)):
                    break
                if not _iter_listing_rows(payload):
                    break
            if _collection_window_row_limit_reached(len(out)):
                break
    finally:
        if signer is not None and owns_signer:
            signer.close()
    return out


def _fetch_c5_via_browser(
    *,
    ids: list[int],
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int,
    request_interval: float,
    cookie: str = "",
    token: str = "",
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    """Collect via minimized system Chrome/Edge sniffing the sell-list XHR.

    Risk / verify / hard failures pause C5 for this run immediately — no retry.
    """
    from core.c5_browser_collect import get_c5_browser_collector

    del cookie, token  # Session injects saved login cookies itself.
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )
    collector = get_c5_browser_collector()
    collector.ensure_open(progress=progress, cancel_check=cancel_check)
    out: list[dict[str, Any]] = []
    request_no = 0
    try:
        for item_id in ids:
            for page in range(1, _effective_page_limit(max_pages) + 1):
                raise_if_cancelled(cancel_check)
                if _collection_window_row_limit_reached(len(out)):
                    break
                if request_no:
                    interruptible_wait(max(1.0, request_interval), cancel_check)
                request_no += 1
                payload = collector.fetch_list_payload(
                    item_id=int(item_id),
                    min_wear=min_wear,
                    max_wear=max_wear,
                    page_no=page,
                    display_name=display_name,
                    progress=progress,
                    cancel_check=cancel_check,
                )
                rows = _rows_from_c5_payload(
                    payload=payload,
                    item_id=item_id,
                    display_name=display_name,
                    min_wear=min_wear,
                    max_wear=max_wear,
                )
                if price_cap is not None:
                    kept = [
                        row
                        for row in rows
                        if float(row.get("price") or 0) <= price_cap
                    ]
                    hit_cap = len(kept) < len(rows)
                    room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                    out.extend(kept[:room])
                    if hit_cap and not kept:
                        break
                else:
                    room = max(0, _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW - len(out))
                    out.extend(rows[:room])
                if _collection_window_row_limit_reached(len(out)):
                    break
                if not _iter_listing_rows(payload):
                    break
            if _collection_window_row_limit_reached(len(out)):
                break
    except C5AccessGateError as exc:
        raise C5PlatformPausedError(
            f"C5GAME 采集失败，本轮已停止该平台：{exc}"
        ) from exc
    return out


def fetch_c5_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 0,
    request_interval: float = 2.0,
    progress: Callable[[str], None] | None = None,
    extra_ids: list[int] | None = None,
    cancel_check: CancelCheck = None,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    """Collect C5 exact-wear listings via minimized system-browser sniffing.

    On failure / risk-control, pause C5 for the rest of this run with no retry.
    """
    wear_windows = _split_wear_windows(min_wear, max_wear)
    raise_if_cancelled(cancel_check)
    if not wear_windows:
        return []
    if not _merge_platform_ids(template.c5, min_wear, max_wear, extra_ids):
        return []
    cookie, token = _c5_auth()
    if not cookie and not token:
        raise RuntimeError("C5GAME 登录已失效，请重新登录后再采集")
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )

    collected: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    for window_low, window_high in wear_windows:
        raise_if_cancelled(cancel_check)
        ids = _merge_platform_ids(
            template.c5,
            window_low,
            window_high,
            extra_ids,
        )
        if not ids:
            continue
        if progress:
            progress(f"C5GAME · 磨损 {window_low:g}–{window_high:g}")
        rows = _fetch_c5_via_browser(
            ids=ids,
            display_name=display_name,
            min_wear=window_low,
            max_wear=window_high,
            max_pages=max_pages,
            request_interval=request_interval,
            cookie=cookie,
            token=token,
            progress=progress,
            cancel_check=cancel_check,
            max_unit_price=price_cap,
        )
        window_kept = 0
        for row in rows:
            if not _in_range(float(row.get("float_value") or -1), min_wear, max_wear):
                continue
            if price_cap is not None and float(row.get("price") or 0) > price_cap:
                continue
            listing_id = str(row.get("listing_id") or "")
            dedupe_key = listing_id or str(row.get("goods_id") or "")
            if not dedupe_key or dedupe_key in seen_listing_ids:
                continue
            if _collection_window_row_limit_reached(window_kept):
                break
            seen_listing_ids.add(dedupe_key)
            collected.append(row)
            window_kept += 1
        if _collection_window_row_limit_reached(window_kept) and progress:
            progress(
                f"C5GAME · {display_name} · 本磨损区间已满 "
                f"{_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW} 条，停止本窗"
            )
    return collected


def _c5_message_has_verify_signal(text: str) -> bool:
    """True when C5 error text clearly indicates human / security verification."""
    raw = str(text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    return any(
        marker in raw or marker in low
        for marker in (
            "风控",
            "虚拟设备",
            "异常网络",
            "高频",
            "风险",
            "滑块",
            "安全验证",
            "安全检查",
            "人机",
            "验证码",
            "captcha",
            "slider",
            "访问校验",
            "请完成验证",
        )
    )


def _c5_error_needs_verify(exc: BaseException | None) -> bool:
    if isinstance(exc, C5AccessGateError):
        return bool(exc.needs_verify) or _c5_message_has_verify_signal(str(exc))
    return _c5_message_has_verify_signal(str(exc or ""))


def _c5_is_access_gate_error(exc: BaseException) -> bool:
    if isinstance(exc, (C5AccessGateError, C5PlatformPausedError)):
        return True
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "暂时不可用",
            "风控",
            "频率",
            "429",
            "滑块",
            "安全验证",
            "napi 不可用",
            "网页接口",
        )
    )


def _c5_access_gate_probe_ok() -> bool:
    """True when the same listing probe used by login validation succeeds."""
    try:
        cookie, token = _c5_auth()
        result = _probe_c5_collection_login(cookie, token, timeout=8.0)
        return bool(result.get("ok"))
    except Exception:
        return False


def _c5_cookie_header_from_items(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _complete_c5_verify_system_browser(
    *,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
) -> None:
    """Open a real Chrome/Edge window (not Playwright) for C5 security checks."""
    from core.market_external_browser import (
        harvest_profile_cookies,
        launch_system_browser,
        wait_browser_closed,
    )

    raise_if_cancelled(cancel_check)
    profile = CACHE_DIR / "market_browser_profiles" / "c5"
    profile.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(
            "C5GAME · 检测到安全验证，已打开系统浏览器；"
            "请完成验证，完成后窗口将自动关闭…"
        )
    try:
        proc = launch_system_browser(
            profile_dir=profile,
            url="https://www.c5game.com/",
        )
    except Exception as exc:  # noqa: BLE001
        raise C5PlatformPausedError(f"无法打开 C5 验证窗口：{exc}") from exc

    def _auto_close() -> bool:
        raise_if_cancelled(cancel_check)
        return _c5_access_gate_probe_ok()

    closed = wait_browser_closed(
        proc,
        timeout_s=300.0,
        progress=progress,
        progress_message="请在 C5 页面完成安全验证（滑块等）；完成后会自动继续采集…",
        auto_close_when=_auto_close,
        auto_close_message="已检测到 C5 接口恢复，正在关闭验证窗口…",
    )
    if not closed:
        raise C5PlatformPausedError("等待 C5 安全验证超时")
    time.sleep(1.0)
    try:
        cookies = harvest_profile_cookies(
            profile,
            domain_hints=("c5game.com", "zbt.com"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("C5 验证后读取 Cookie 失败：%s", exc)
        return
    cookie = _c5_cookie_header_from_items(cookies)
    token = ""
    for item in cookies:
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name in {
            "nc5_accesstoken",
            "c5token",
            "access_token",
            "ncaccess",
            "token",
            "authorization",
        } and len(value) > len(token):
            token = value
    if cookie or token:
        try:
            save_c5_auth(cookie, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("C5 验证后保存凭证失败：%s", exc)


def _resolve_c5_access_gate(
    *,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
    request_interval: float = 5.0,
    gate_error: BaseException | None = None,
) -> None:
    """Clear C5 access limits without unnecessary popups.

    - Clear verify signal → system-browser window, wait for user, auto-close.
    - Otherwise → silent wait/retry up to 2 rounds; still blocked → pause platform.
    """
    interval = max(5.0, float(request_interval or 5.0))
    silent_rounds = 2
    needs_verify = _c5_error_needs_verify(gate_error)

    def _silent_wait_rounds() -> bool:
        for round_i in range(1, silent_rounds + 1):
            raise_if_cancelled(cancel_check)
            if _c5_access_gate_probe_ok():
                if progress:
                    progress("C5GAME · 访问限制已解除，继续静默采集…")
                return True
            if progress:
                progress(
                    f"C5GAME · 接口暂时不可用，后台等待 {interval:.0f}s 后重试"
                    f"（{round_i}/{silent_rounds}）…"
                )
            interruptible_wait(interval, cancel_check)
        return _c5_access_gate_probe_ok()

    if needs_verify:
        _complete_c5_verify_system_browser(
            progress=progress,
            cancel_check=cancel_check,
        )
        if _c5_access_gate_probe_ok():
            if progress:
                progress("C5GAME · 安全验证通过，继续采集…")
            return
        logger.info("C5GAME 验证窗口结束后探针仍失败（此前：%s）", gate_error)
        raise C5PlatformPausedError(
            "C5GAME 安全验证后仍不可用，本轮已暂停该平台采集"
        )

    if _silent_wait_rounds():
        return

    logger.info(
        "C5GAME 静默重试 %s 轮后仍受限（此前：%s），本轮暂停平台",
        silent_rounds,
        gate_error,
    )
    raise C5PlatformPausedError(
        f"C5GAME 访问受限，已静默重试 {silent_rounds} 轮仍失败，"
        "本轮已暂停该平台采集"
    )


def _eco_sell_query_body(
    *,
    goods_id: int = 0,
    hash_name: str = "",
    min_wear: float,
    max_wear: float,
    page: int,
    page_size: int = 40,
) -> dict[str, Any]:
    """Build ECO website SellGoodsQuery payload.

    Prefer HashName: current ECO API returns「商品不存在」for many GoodsId-only
    queries, while the same item succeeds with Steam market HashName.
    """
    body: dict[str, Any] = {
        "GameId": "730",
        "PageIndex": max(1, int(page)),
        "PageSize": max(1, min(100, int(page_size))),
        # Live probe: SortType=1 + Sort=0 returns price ascending (page + across pages).
        # Sort=1 is NOT price-asc and must not be used with 2.5× early-stop.
        "SortType": 1,
        "Sort": 0,
        "TradeType": 1,
        # Same wear field names as open-platform SellGoodsList.
        "StartPaintWear": float(min_wear),
        "EndPaintWear": float(max_wear),
    }
    hash_name = str(hash_name or "").strip()
    if hash_name:
        body["HashName"] = hash_name
    elif int(goods_id or 0) > 0:
        body["GoodsId"] = int(goods_id)
    return body


def _post_eco_sell_query(
    *,
    goods_id: int = 0,
    hash_name: str = "",
    min_wear: float,
    max_wear: float,
    page: int,
    cookie: str,
    token: str,
) -> dict[str, Any]:
    refer_id = int(goods_id or 0) or "0"
    headers = _eco_request_headers(goods_id=refer_id, cookie=cookie, token=token)
    headers["Content-Type"] = "application/json; charset=utf-8"
    headers["Accept"] = "application/json, text/plain, */*"
    body = _eco_sell_query_body(
        goods_id=goods_id,
        hash_name=hash_name,
        min_wear=min_wear,
        max_wear=max_wear,
        page=page,
    )
    if "HashName" not in body and "GoodsId" not in body:
        raise RuntimeError("ECOSteam 缺少 HashName / GoodsId，无法查询在售")
    last_error: Exception | None = None
    for url in _ECO_SELL_QUERY_APIS:
        try:
            response = requests.post(url, headers=headers, json=body, timeout=18)
        except requests.RequestException as exc:
            last_error = exc
            continue
        if response.status_code == 429:
            raise EcoAccessGateError(
                "ECOSteam 返回访问频率过高",
                result_msg="频率过高",
                needs_slider=False,
            )
        if response.status_code in {401, 403}:
            raise RuntimeError("ECOSteam 登录已失效，请重新登录后再采集")
        if response.status_code == 404:
            last_error = RuntimeError(f"ECOSteam 在售接口不存在：{url}")
            continue
        try:
            payload = response.json() if response.text else {}
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError("ECOSteam 在售接口返回异常") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("ECOSteam 在售接口返回异常")
        if response.status_code >= 400:
            body_payload = _eco_result_payload(payload)
            detail = (
                body_payload.get("ResultMsg")
                or body_payload.get("StatusMsg")
                or payload.get("StatusMsg")
                or f"HTTP {response.status_code}"
            )
            raise RuntimeError(f"ECOSteam 在售接口失败：{detail}")
        return payload
    if last_error is not None:
        raise RuntimeError(f"ECOSteam 在售接口请求失败：{last_error}") from last_error
    raise RuntimeError("ECOSteam 在售接口请求失败")


def _eco_is_access_gate_error(exc: BaseException) -> bool:
    if isinstance(exc, (EcoAccessGateError, EcoPlatformPausedError)):
        return True
    text = str(exc)
    return any(marker in text for marker in ("校验", "429", "滑块", "频率"))


def _eco_message_has_slider_signal(text: str) -> bool:
    """True when ECO response text clearly indicates a human slider challenge."""
    raw = str(text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if any(
        marker in low
        for marker in (
            "slider",
            "frequent_slider",
            "滑块",
            "拖动",
            "人机",
            "captcha",
        )
    ):
        return True
    # GUID / challenge token (not a plain numeric code like 429).
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw):
        return True
    if re.fullmatch(r"[0-9a-zA-Z_-]{12,}", raw) and not raw.isdigit():
        return True
    return False


def _eco_gate_error_needs_slider(exc: BaseException | None) -> bool:
    if isinstance(exc, EcoAccessGateError):
        return bool(exc.needs_slider) or _eco_message_has_slider_signal(
            exc.result_msg or str(exc)
        )
    return _eco_message_has_slider_signal(str(exc or ""))


def _eco_payload_is_access_gate(payload: dict[str, Any]) -> bool:
    body_payload = _eco_result_payload(payload)
    result_code = str(
        body_payload.get("ResultCode")
        or body_payload.get("resultCode")
        or payload.get("ResultCode")
        or ""
    )
    return result_code == "429"


def _eco_access_gate_probe_ok() -> bool:
    """True when a SellGoodsQuery probe succeeds (gate / rate-limit cleared)."""
    try:
        token, cookie = _eco_auth()
        payload = _post_eco_sell_query(
            goods_id=_ECO_LOGIN_CHECK_GOODS_ID,
            hash_name="",
            min_wear=_LOGIN_CHECK_MIN_WEAR,
            max_wear=_LOGIN_CHECK_MAX_WEAR,
            page=1,
            cookie=cookie,
            token=token,
        )
        if not isinstance(payload, dict) or _eco_payload_is_access_gate(payload):
            return False
        return True
    except Exception:
        return False


def _resolve_eco_access_gate(
    *,
    progress: Callable[[str], None] | None = None,
    cancel_check: CancelCheck = None,
    request_interval: float = 3.0,
    gate_error: BaseException | None = None,
) -> None:
    """Clear ECO access limits without unnecessary popups.

    - Clear slider signal → open headed window, wait for user, auto-close.
    - Otherwise → silent wait/retry up to 3 rounds; still blocked → pause platform.
    """
    interval = max(3.0, float(request_interval or 3.0))
    silent_rounds = 3
    needs_slider = _eco_gate_error_needs_slider(gate_error)

    def _silent_wait_rounds(label: str) -> bool:
        for round_i in range(1, silent_rounds + 1):
            raise_if_cancelled(cancel_check)
            if _eco_access_gate_probe_ok():
                if progress:
                    progress("ECOSteam · 访问限制已解除，继续静默采集…")
                return True
            if progress:
                progress(
                    f"ECOSteam · {label}，后台等待 {interval:.0f}s 后重试"
                    f"（{round_i}/{silent_rounds}）…"
                )
            interruptible_wait(interval, cancel_check)
        return _eco_access_gate_probe_ok()

    if needs_slider:
        if progress:
            progress("ECOSteam · 检测到滑块验证，验证窗口已开到任务栏…")
        from core.market_access_session import close_access_sessions, get_access_session

        session = get_access_session("eco")
        try:
            raw = session.complete_eco_access_gate(
                progress=progress,
                cancel_check=cancel_check,
                require_slider=True,
            )
            outcome = str(raw or "cleared")
        except CollectionCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("ECOSteam 滑块验证失败：%s", exc)
            raise EcoPlatformPausedError(
                f"ECOSteam 滑块验证未完成，本轮已暂停该平台采集：{exc}"
            ) from exc
        finally:
            close_access_sessions("eco")
        if outcome == "cleared" or _eco_access_gate_probe_ok():
            if progress:
                progress("ECOSteam · 滑块验证通过，继续采集…")
            return
        logger.info(
            "ECOSteam 滑块窗口未通过（outcome=%s），本轮暂停平台",
            outcome,
        )
        raise EcoPlatformPausedError(
            "ECOSteam 滑块验证未通过，本轮已暂停该平台采集"
        )

    if _silent_wait_rounds("接口访问受限（无需滑块）"):
        return

    logger.info(
        "ECOSteam 静默重试 %s 轮后仍受限（此前：%s），本轮暂停平台",
        silent_rounds,
        gate_error,
    )
    raise EcoPlatformPausedError(
        f"ECOSteam 访问受限，已静默重试 {silent_rounds} 轮仍失败，"
        "本轮已暂停该平台采集"
    )


def fetch_eco_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 0,
    request_interval: float = 2.0,
    progress: Callable[[str], None] | None = None,
    extra_ids: list[int] | None = None,
    cancel_check: CancelCheck = None,
    silent: bool = False,
    max_unit_price: float | None = None,
) -> list[dict[str, Any]]:
    wear_windows = _split_wear_windows(min_wear, max_wear)
    if not wear_windows:
        return []
    token, cookie = _eco_auth()
    page_limit = _effective_page_limit(max_pages)
    price_cap = (
        float(max_unit_price)
        if max_unit_price is not None and float(max_unit_price) > 0
        else None
    )

    def _filter_window_rows(
        page_rows: list[dict[str, Any]],
        *,
        window_low: float,
        window_high: float,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Keep in-window rows under price_cap; second value is page_hit_cap."""
        kept: list[dict[str, Any]] = []
        page_hit_cap = False
        for row in page_rows:
            try:
                wear = float(row.get("float_value") or -1)
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if not _in_range(wear, window_low, window_high):
                continue
            if price_cap is not None and price > price_cap:
                page_hit_cap = True
                continue
            kept.append(row)
        return kept, page_hit_cap

    def _collect_via_api(
        *,
        auth_token: str,
        auth_cookie: str,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        seen_listing_ids: set[str] = set()
        request_no = 0

        def _append_rows(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                listing_id = str(row.get("listing_id") or "")
                dedupe_key = listing_id or str(row.get("goods_id") or "")
                if not dedupe_key or dedupe_key in seen_listing_ids:
                    continue
                seen_listing_ids.add(dedupe_key)
                collected.append(row)

        for window_low, window_high in wear_windows:
            raise_if_cancelled(cancel_check)
            window_start = len(collected)
            targets = _overlapping_steam_targets(template, window_low, window_high)
            if not targets:
                for goods_id in _merge_platform_ids(
                    template.eco,
                    window_low,
                    window_high,
                    extra_ids,
                ):
                    targets.append(("", int(goods_id)))
            for hash_name, goods_id in targets:
                raise_if_cancelled(cancel_check)
                if _collection_window_row_limit_reached(
                    len(collected) - window_start
                ):
                    break
                skip_goods = False
                for page in range(1, page_limit + 1):
                    raise_if_cancelled(cancel_check)
                    if _collection_window_row_limit_reached(
                        len(collected) - window_start
                    ):
                        break
                    if request_no:
                        interruptible_wait(max(1.0, request_interval), cancel_check)
                    request_no += 1
                    if progress:
                        progress(
                            "ECOSteam · "
                            f"{display_name} · 磨损 {window_low:g}–{window_high:g} · "
                            f"第 {page} 页"
                        )
                    try:
                        payload = _post_eco_sell_query(
                            goods_id=goods_id,
                            hash_name=hash_name,
                            min_wear=window_low,
                            max_wear=window_high,
                            page=page,
                            cookie=auth_cookie,
                            token=auth_token,
                        )
                        raise_if_cancelled(cancel_check)
                        page_rows = _rows_from_eco_payload(
                            payload=payload,
                            goods_id=goods_id,
                            display_name=display_name,
                            min_wear=min_wear,
                            max_wear=max_wear,
                        )
                    except RuntimeError as exc:
                        text = str(exc)
                        if "商品不存在" in text:
                            if progress:
                                label = hash_name or f"ID {goods_id}"
                                progress(
                                    f"ECOSteam · 跳过无效商品 {label}"
                                    f"（{display_name}）"
                                )
                            skip_goods = True
                            break
                        raise
                    page_rows, page_hit_cap = _filter_window_rows(
                        page_rows,
                        window_low=window_low,
                        window_high=window_high,
                    )
                    room = max(
                        0,
                        _COLLECTION_MAX_ROWS_PER_WEAR_WINDOW
                        - (len(collected) - window_start),
                    )
                    before = len(collected)
                    _append_rows(page_rows[:room])
                    page_kept = len(collected) - before
                    if _collection_window_row_limit_reached(
                        len(collected) - window_start
                    ):
                        if progress:
                            progress(
                                f"ECOSteam · {display_name} · 本磨损区间已满 "
                                f"{_COLLECTION_MAX_ROWS_PER_WEAR_WINDOW} 条，停止本窗"
                            )
                        break
                    # Price-asc pages: entire page above recipe cap → stop paging.
                    if price_cap is not None and page_hit_cap and page_kept == 0:
                        if progress:
                            progress(
                                f"ECOSteam · {display_name} · 已超过配方单价 "
                                f"{_COLLECTION_ECO_PRICE_CAP_MULTIPLIER:g} 倍，"
                                "停止本窗翻页"
                            )
                        break
                    if not _iter_listing_rows(payload):
                        break
                if skip_goods:
                    continue
        return collected

    try:
        return _collect_via_api(auth_token=token, auth_cookie=cookie)
    except CollectionCancelled:
        raise
    except EcoPlatformPausedError:
        raise
    except RuntimeError as exc:
        if not _eco_is_access_gate_error(exc):
            raise
        gate_error = exc

    try:
        _resolve_eco_access_gate(
            progress=progress,
            cancel_check=cancel_check,
            request_interval=request_interval,
            gate_error=gate_error,
        )
    except CollectionCancelled:
        raise
    except EcoPlatformPausedError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EcoPlatformPausedError(
            f"{exc}（此前：{gate_error}）"
        ) from exc

    token, cookie = _eco_auth()
    try:
        return _collect_via_api(auth_token=token, auth_cookie=cookie)
    except CollectionCancelled:
        raise
    except EcoPlatformPausedError:
        raise
    except RuntimeError as exc:
        if _eco_is_access_gate_error(exc):
            raise EcoPlatformPausedError(
                f"ECOSteam 访问校验后仍受限，本轮已暂停该平台采集"
                f"（此前：{gate_error}）"
            ) from exc
        raise


def fetch_exact_wear_candidates(
    provider: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    cancel_check = kwargs.get("cancel_check")
    raise_if_cancelled(cancel_check)
    if not provider_auth_available(provider):
        raise RuntimeError(
            f"APP 未获取 {provider_display_name(provider)} 登录凭证；"
            "系统浏览器登录态不能直接用于采集，请先完成共享登录"
        )
    template = kwargs.get("template")
    extra_ids = _coerce_positive_ids(kwargs.get("extra_ids"))
    kwargs["extra_ids"] = extra_ids
    # ``silent`` / ``unit_price_cny`` are routing options, not shared by every fetcher.
    silent = bool(kwargs.pop("silent", False))
    unit_price_cny = kwargs.pop("unit_price_cny", None)
    if provider in {"buff", "yyyp", "c5"}:
        price_cap = _collection_max_unit_price(unit_price_cny)
    elif provider == "eco":
        price_cap = _collection_max_unit_price(
            unit_price_cny,
            multiplier=_COLLECTION_ECO_PRICE_CAP_MULTIPLIER,
        )
    else:
        price_cap = None
    if price_cap is not None:
        kwargs["max_unit_price"] = price_cap
    else:
        kwargs.pop("max_unit_price", None)
    cache_key = (
        provider,
        str(getattr(template, "paint_index", "")),
        round(float(kwargs.get("min_wear") or 0), 8),
        round(float(kwargs.get("max_wear") or 1), 8),
        int(kwargs.get("max_pages") or 0),
        tuple(extra_ids),
        round(float(price_cap), 4) if price_cap is not None else None,
    )
    cached = _candidate_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CANDIDATE_CACHE_TTL_SECONDS:
        raise_if_cancelled(cancel_check)
        return [dict(item) for item in cached[1]]
    interval_floor = 5.0 if provider == "c5" else 3.0
    kwargs["request_interval"] = max(
        interval_floor,
        float(kwargs.get("request_interval") or interval_floor),
    )
    if provider == "buff":
        result = fetch_buff_candidates(**kwargs)
    elif provider == "yyyp":
        result = fetch_youpin_candidates(**kwargs)
    elif provider == "c5":
        result = fetch_c5_candidates(**kwargs)
    elif provider == "eco":
        result = fetch_eco_candidates(silent=silent, **kwargs)
    else:
        raise RuntimeError(f"平台 {provider} 暂不支持精确磨损挂单采集")
    raise_if_cancelled(cancel_check)
    # Do not cache empty results: transient empty responses (soft rate-limit /
    # brief stock gaps) would otherwise poison the next 180s for the same key.
    if result:
        _candidate_cache[cache_key] = (now, [dict(item) for item in result])
    return result
