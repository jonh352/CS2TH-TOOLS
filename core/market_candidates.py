"""Low-frequency exact-wear listing collection for trade-up candidates."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from config import CACHE_DIR
from core.data_utils import SkinTemplate

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BUFF_API = "https://buff.163.com/api/market/goods/sell_order"
_BUFF_LOGIN_CHECK_GOODS_ID = 956527
_YOUPIN_API = (
    "https://api.youpin898.com/api/homepage/es/commodity/GetCsGoPagedList"
)
_YOUPIN_USER_INFO_API = (
    "https://api.youpin898.com/api/user/Account/GetUserInfo"
)
EXACT_WEAR_PROVIDERS = frozenset({"buff", "yyyp"})
_CANDIDATE_CACHE_TTL_SECONDS = 180.0
_candidate_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}

_WEAR_BOUNDS = {
    "崭新出厂": (0.0, 0.07),
    "略有磨损": (0.07, 0.15),
    "久经沙场": (0.15, 0.38),
    "破损不堪": (0.38, 0.45),
    "战痕累累": (0.45, 1.0),
}


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


def provider_auth_available(provider: str) -> bool:
    if provider == "buff":
        return "session=" in _buff_cookie().lower()
    if provider == "yyyp":
        token, _cookie = _youpin_auth()
        return bool(token)
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


def _validate_buff_login(*, timeout: float) -> dict[str, Any]:
    cookie = _buff_cookie()
    if "session=" not in cookie.lower():
        return _validation_result(
            "buff",
            ok=False,
            message="APP 未获取 BUFF 登录凭证",
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
                "sort_by": "default",
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
    if not token:
        return _validation_result(
            "yyyp",
            ok=False,
            message="APP 未获取悠悠有品登录凭证",
        )
    return validate_youpin_credentials(token, cookie, timeout=timeout)


def validate_youpin_credentials(
    token: str,
    cookie: str = "",
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Validate a freshly captured Youpin token before persisting it."""
    token = str(token or "").strip()
    cookie = str(cookie or "").strip()
    if not token:
        return _validation_result(
            "yyyp",
            ok=False,
            message="未捕获到悠悠有品 Token",
        )
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
    try:
        response = requests.get(
            _YOUPIN_USER_INFO_API,
            headers=headers,
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
        return _validation_result(
            "yyyp",
            ok=False,
            indeterminate=True,
            message=f"悠悠有品校验请求失败：{exc}",
        )
    code = int(payload.get("Code") or payload.get("code") or 0)
    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    user_id = str(
        data.get("UserId")
        or data.get("userId")
        or data.get("Id")
        or data.get("id")
        or ""
    ).strip()
    nickname = str(
        data.get("NickName")
        or data.get("nickName")
        or data.get("Nickname")
        or data.get("nickname")
        or data.get("UserName")
        or data.get("userName")
        or ""
    ).strip()
    if code == 0 and user_id and nickname:
        return _validation_result(
            "yyyp",
            ok=True,
            message="悠悠有品登录有效",
            account_name=nickname,
            user_id=user_id,
        )
    detail = str(
        payload.get("Msg")
        or payload.get("msg")
        or "登录凭证无效"
    )
    return _validation_result(
        "yyyp",
        ok=False,
        message=f"悠悠有品登录已失效：{detail}",
    )


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


def _in_range(value: float, low: float, high: float) -> bool:
    return low <= value <= high if high >= 1.0 else low <= value < high


def fetch_buff_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 2,
    request_interval: float = 5.0,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    ids = _overlapping_ids(template.buff, min_wear, max_wear)
    if not ids:
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
    request_no = 0
    for goods_id in ids:
        for page in range(1, max(1, max_pages) + 1):
            if request_no:
                time.sleep(max(1.0, request_interval))
            request_no += 1
            if progress:
                progress(f"BUFF · {display_name} · 第 {page} 页")
            response = requests.get(
                _BUFF_API,
                params={
                    "game": "csgo",
                    "goods_id": goods_id,
                    "page_num": page,
                    "sort_by": "default",
                },
                headers=headers,
                timeout=18,
            )
            if response.status_code == 429:
                raise RuntimeError("BUFF 返回访问频率过高，已立即停止本平台采集")
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "OK":
                detail = payload.get("error") or payload.get("code") or "未知错误"
                raise RuntimeError(f"BUFF 在售接口失败：{detail}")
            items = (payload.get("data") or {}).get("items") or []
            if not items:
                break
            for item in items:
                raw_wear = (item.get("asset_info") or {}).get("paintwear")
                try:
                    wear = float(raw_wear)
                    price = float(item.get("price"))
                except (TypeError, ValueError):
                    continue
                if price <= 0 or not _in_range(wear, min_wear, max_wear):
                    continue
                order_id = str(item.get("id") or "")
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
    return out


def fetch_youpin_candidates(
    *,
    template: SkinTemplate,
    display_name: str,
    min_wear: float,
    max_wear: float,
    max_pages: int = 2,
    request_interval: float = 5.0,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    ids = _overlapping_ids(template.yyyp, min_wear, max_wear)
    if not ids:
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
    request_no = 0
    for template_id in ids:
        for page in range(1, max(1, max_pages) + 1):
            if request_no:
                time.sleep(max(1.0, request_interval))
            request_no += 1
            if progress:
                progress(f"悠悠有品 · {display_name} · 第 {page} 页")
            response = requests.post(
                _YOUPIN_API,
                headers=headers,
                json={
                    "templateId": str(template_id),
                    "pageSize": 40,
                    "pageIndex": page,
                    "sortType": 1,
                    "listSortType": 1,
                    "listType": 10,
                    "stickersIsSort": False,
                },
                timeout=18,
            )
            if response.status_code == 429:
                raise RuntimeError("悠悠有品返回访问频率过高，已立即停止本平台采集")
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("Code") or 0) != 0:
                raise RuntimeError(
                    f"悠悠有品在售接口失败：{payload.get('Msg') or payload.get('Code')}"
                )
            items = (payload.get("Data") or {}).get("CommodityList") or []
            if not items:
                break
            for item in items:
                try:
                    wear = float(item.get("Abrade"))
                    price = float(item.get("Price"))
                except (TypeError, ValueError):
                    continue
                if price <= 0 or not _in_range(wear, min_wear, max_wear):
                    continue
                listing_id = str(item.get("Id") or item.get("CommodityNo") or "")
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
                            f"?listType=10&templateId={quote(str(template_id))}&gameId=730"
                        ),
                    }
                )
    return out


def fetch_exact_wear_candidates(
    provider: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    if not provider_auth_available(provider):
        platform_name = "BUFF" if provider == "buff" else "悠悠有品"
        raise RuntimeError(
            f"APP 未获取 {platform_name} 登录凭证；"
            "系统浏览器登录态不能直接用于采集，请先完成共享登录"
        )
    template = kwargs.get("template")
    cache_key = (
        provider,
        str(getattr(template, "paint_index", "")),
        round(float(kwargs.get("min_wear") or 0), 8),
        round(float(kwargs.get("max_wear") or 1), 8),
        int(kwargs.get("max_pages") or 2),
    )
    cached = _candidate_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CANDIDATE_CACHE_TTL_SECONDS:
        return [dict(item) for item in cached[1]]
    kwargs["request_interval"] = max(
        5.0,
        float(kwargs.get("request_interval") or 5.0),
    )
    if provider == "buff":
        result = fetch_buff_candidates(**kwargs)
    elif provider == "yyyp":
        result = fetch_youpin_candidates(**kwargs)
    else:
        raise RuntimeError(f"平台 {provider} 暂不支持精确磨损挂单采集")
    _candidate_cache[cache_key] = (now, [dict(item) for item in result])
    return result
