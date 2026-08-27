"""cs2th.cn account client with Windows-protected local sessions."""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from config import (
    APP_HTTP_USER_AGENT,
    APP_VERSION,
    AUTH_API_BASE_URL,
    AUTH_API_ENABLED,
    AUTH_HTTP_TIMEOUT_S,
    AUTH_SESSION_FILE,
)
from core.client_update import ClientUpdateInfo, parse_client_update

CLIENT_ID = "cs2th-tools"


class AuthUnavailableError(RuntimeError):
    pass


class AuthRejectedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Account:
    user_id: str
    username: str
    member: bool = False
    member_until: float = 0.0
    free_max_cost: float = 0.0
    subscriptions: dict[str, float] | None = None
    effective_entitlements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthSession:
    access_token: str
    account: Account
    public_beta: dict[str, bool] = field(default_factory=dict)
    client_update: ClientUpdateInfo | None = None

    def has_tradeup_access(self) -> bool:
        beta = self.public_beta or {}
        return bool(self.account.member or beta.get("tradeup"))


def has_tradeup_access(session: AuthSession | None) -> bool:
    """Public beta grants access only to a currently logged-in account."""
    return bool(session and session.has_tradeup_access())


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _local_free(pointer) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(pointer)


def _protect_token(token: str) -> str:
    raw = token.encode("utf-8")
    if sys.platform != "win32":
        return "plain:" + base64.b64encode(raw).decode("ascii")
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(
        len(raw),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    protected = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "CS2TH Tools account session",
        None,
        None,
        None,
        0,
        ctypes.byref(protected),
    ):
        raise OSError("无法使用 Windows DPAPI 加密登录状态")
    try:
        encrypted = ctypes.string_at(protected.pbData, protected.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        _local_free(protected.pbData)


def _unprotect_token(value: str) -> str:
    if value.startswith("plain:"):
        return base64.b64decode(value[6:]).decode("utf-8")
    if not value.startswith("dpapi:") or sys.platform != "win32":
        return ""
    encrypted = base64.b64decode(value[6:])
    buffer = ctypes.create_string_buffer(encrypted)
    source = _DataBlob(
        len(encrypted),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    plain = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(plain),
    ):
        return ""
    try:
        return ctypes.string_at(plain.pbData, plain.cbData).decode("utf-8")
    finally:
        _local_free(plain.pbData)


def _account_from_payload(payload: dict[str, Any]) -> Account:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    is_serialized_account = (
        user is payload and "user_id" in user and "member" in user
    )
    subscriptions = user.get("subscriptions")
    subscriptions = subscriptions if isinstance(subscriptions, dict) else {}
    effective_raw = user.get("effective_entitlements")
    has_entitlement_schema = isinstance(effective_raw, list)
    effective = tuple(str(item) for item in effective_raw) if has_entitlement_schema else ()
    has_tradeup = "*" in effective or "tradeup" in effective
    def subscription_until(key: str) -> float:
        state = subscriptions.get(key)
        raw = state.get("expires_at") if isinstance(state, dict) else state
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    relevant_until = max(subscription_until("tradeup"), subscription_until("all_access"))
    return Account(
        user_id=str(user.get("id") or user.get("user_id") or ""),
        username=str(user.get("username") or "").strip(),
        member=(
            bool(user.get("member"))
            if is_serialized_account
            else has_tradeup
            if has_entitlement_schema
            else bool(user.get("is_member", user.get("member", False)))
        ),
        member_until=relevant_until or float(user.get("member_until") or 0.0),
        free_max_cost=float(user.get("free_max_cost") or 0.0),
        subscriptions={
            str(k): subscription_until(str(k))
            for k in subscriptions
        },
        effective_entitlements=effective,
    )


def _response_detail(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        return fallback
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return fallback


def _session_from_payload(access_token: str, payload: dict[str, Any]) -> AuthSession:
    public_beta = payload.get("public_beta")
    public_beta = public_beta if isinstance(public_beta, dict) else {}
    if "tradeup" not in public_beta and "login_full_access" in payload:
        public_beta["tradeup"] = bool(payload.get("login_full_access"))
    return AuthSession(
        access_token,
        _account_from_payload(payload),
        {str(key): bool(value) for key, value in public_beta.items()},
        parse_client_update(payload),
    )


class AuthClient:
    def __init__(
        self,
        *,
        enabled: bool = AUTH_API_ENABLED,
        base_url: str = AUTH_API_BASE_URL,
        session_file: Path = AUTH_SESSION_FILE,
        http_session: requests.Session | None = None,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.session_file = session_file
        self._http = http_session or requests.Session()
        self._http.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": APP_HTTP_USER_AGENT,
                "X-CS2TH-Client": CLIENT_ID,
                "X-CS2TH-Version": APP_VERSION,
            }
        )

    def load_local_session(self) -> AuthSession | None:
        try:
            raw = json.loads(self.session_file.read_text(encoding="utf-8"))
            protected = str(raw.get("protected_token") or "")
            token = _unprotect_token(protected) if protected else str(
                raw.get("access_token") or raw.get("session_token") or ""
            )
            account = _account_from_payload(raw.get("account") or {})
            if token and account.username:
                # Public-beta state is deliberately not restored from disk.
                # It must be fetched again from cs2th.cn on every app start.
                session = AuthSession(token, account)
                if not protected:
                    self._save(session)
                return session
        except Exception:
            pass
        return None

    def _save(self, session: AuthSession) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(self.session_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "protected_token": _protect_token(session.access_token),
                    "account": asdict(session.account),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.session_file)

    def clear_local_session(self) -> None:
        try:
            self.session_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _login_token(self, response: requests.Response, payload: dict) -> str:
        token = str(
            payload.get("session_token") or payload.get("access_token") or ""
        ).strip()
        if token:
            return token
        # The currently deployed cs2th.cn schema establishes successful web
        # sessions only through Set-Cookie. Accept that cookie as the Bearer
        # token so the desktop client remains compatible during deployment.
        for cookie_jar in (
            getattr(response, "cookies", None),
            getattr(self._http, "cookies", None),
        ):
            if cookie_jar is None:
                continue
            for name in ("cs2th_session", "session_token"):
                try:
                    value = str(cookie_jar.get(name) or "").strip()
                except (AttributeError, KeyError, TypeError):
                    value = ""
                if value:
                    return value
        return ""

    def login(self, username: str, password: str) -> AuthSession:
        if not self.enabled:
            raise AuthUnavailableError("CS2TH 账号服务当前未启用")
        try:
            response = self._http.post(
                f"{self.base_url}/api/auth/login",
                json={
                    "username": username.strip(),
                    "password": password,
                    "client": CLIENT_ID,
                },
                timeout=AUTH_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise AuthUnavailableError("无法连接 CS2TH 账号服务") from exc
        if response.status_code in (400, 401, 403):
            raise AuthRejectedError(
                _response_detail(response, "用户名或密码不正确")
            )
        try:
            response.raise_for_status()
            payload = response.json()
            session = _session_from_payload(
                self._login_token(response, payload),
                payload,
            )
        except (AttributeError, TypeError, ValueError, requests.RequestException) as exc:
            raise AuthUnavailableError("账号服务返回了无效响应") from exc
        if not session.account.username:
            raise AuthUnavailableError("账号服务登录成功，但未返回用户信息")
        if not session.access_token:
            raise AuthUnavailableError(
                "账号服务登录成功，但未返回 session_token 或会话 Cookie"
            )
        self._save(session)
        return session

    def validate_session(self, session: AuthSession) -> AuthSession | None:
        if not self.enabled:
            return session
        try:
            response = self._http.get(
                f"{self.base_url}/api/auth/me",
                headers={"Authorization": f"Bearer {session.access_token}"},
                timeout=AUTH_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise AuthUnavailableError("暂时无法验证 CS2TH 登录状态") from exc
        if response.status_code in (401, 403):
            self.clear_local_session()
            return None
        try:
            response.raise_for_status()
            payload = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise AuthUnavailableError("账号服务返回了无效会话状态") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("user"), dict):
            self.clear_local_session()
            return None
        refreshed = _session_from_payload(session.access_token, payload)
        if not refreshed.account.username:
            self.clear_local_session()
            return None
        self._save(refreshed)
        return refreshed

    def logout(self, session: AuthSession | None) -> None:
        if self.enabled and session is not None:
            try:
                self._http.post(
                    f"{self.base_url}/api/auth/logout",
                    headers={"Authorization": f"Bearer {session.access_token}"},
                    timeout=AUTH_HTTP_TIMEOUT_S,
                )
            except requests.RequestException:
                pass
        self.clear_local_session()
