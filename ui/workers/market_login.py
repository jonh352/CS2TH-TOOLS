"""Marketplace login capture and credential validation workers."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import sync_playwright

from config import CACHE_DIR
from core.market_candidates import (
    save_buff_auth,
    save_c5_auth,
    save_c5_client_headers,
    save_eco_auth,
    save_youpin_auth,
    validate_c5_credentials,
    validate_eco_credentials,
    validate_provider_login,
    validate_youpin_credentials,
)
from core.steam.errors import SteamBrowserLaunchError, SteamBrowserNotFoundError
from core.steam.launch import focus_single_page, launch_persistent_chromium_context

_LOGIN_URLS = {
    "buff": "https://buff.163.com/account/login",
    "yyyp": "https://www.youpin898.com/",
    "c5": "https://www.c5game.com/login",
    # ECO has no dedicated /login route; login is a homepage modal (.loginBtn).
    "eco": "https://www.ecosteam.cn/",
}


def _cookie_header(cookies: list[dict[str, Any]], *domain_hints: str) -> str:
    hints = [hint.lower() for hint in domain_hints if hint]
    if not hints:
        hints = [""]
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip()
        if not name or name in seen:
            continue
        if hints != [""] and not any(hint in domain for hint in hints):
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def _loose_token(value: Any) -> str:
    token = str(value or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    match = re.match(r"^Bearer\s+(.+)$", token, flags=re.IGNORECASE)
    if match:
        token = match.group(1).strip()
    if not (12 <= len(token) <= 8192):
        return ""
    # UUID / Youpin device-id shaped values are not login tokens.
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", token, re.I):
        return ""
    if re.fullmatch(r"[0-9a-f]{32}-\d+-\d{10,}-\d{10,}", token, re.I):
        return ""
    if re.fullmatch(r"[0-9]+", token):
        return ""
    return token


def _normalized_token(value: Any) -> str:
    token = _loose_token(value)
    if not token or len(token) < 20 or len(token) > 4096:
        return ""
    return token


def _tokens_from_storage(raw: Any, *, loose: bool = False) -> list[str]:
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    normalize = _loose_token if loose else _normalized_token

    def add(value: Any, score: int) -> None:
        token = normalize(value)
        if token and token not in seen:
            seen.add(token)
            candidates.append((score, token))

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 4:
            return
        score = 100 if re.search(r"token|authorization|auth|access", key, re.I) else 5
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


# Intercept XHR/fetch Authorization headers and login response Token bodies.
_YOUPIN_HOOK_JS = r"""
(() => {
  if (window.__cs2thYpHooked) return true;
  window.__cs2thYpHooked = true;
  window.__cs2thYpAuth = window.__cs2thYpAuth || '';
  window.__cs2thYpAuthSource = window.__cs2thYpAuthSource || '';
  function looksToken(s) {
    s = String(s || '').trim();
    if (s.length < 20 || s.length > 2048) return false;
    if (/^[0-9a-f]{32}-\d+-\d{10,}-\d{10,}$/i.test(s)) return false;
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s)) return false;
    return true;
  }
  function capture(h, source) {
    if (!h) return;
    let s = String(h).trim();
    const m = s.match(/^Bearer\s+(.+)$/i);
    if (m) s = m[1].trim();
    if (!looksToken(s)) return;
    window.__cs2thYpAuth = s;
    window.__cs2thYpAuthSource = String(source || '');
  }
  function digToken(obj) {
    if (!obj || typeof obj !== 'object') return '';
    const d = obj.Data || obj.data || obj;
    for (const k of ['Token', 'token', 'accessToken', 'AccessToken', 'authorization', 'Authorization']) {
      if (d && d[k] && looksToken(d[k])) return String(d[k]).trim();
    }
    return '';
  }
  try {
    const xo = XMLHttpRequest.prototype.open;
    const xs = XMLHttpRequest.prototype.send;
    const xh = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function (method, url) {
      try { this.__cs2thUrl = String(url || ''); } catch (e) {}
      return xo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      try {
        if (/^authorization$/i.test(String(k || ''))) capture(v, 'xhr-header:' + (this.__cs2thUrl || ''));
      } catch (e) {}
      return xh.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      try {
        this.addEventListener('load', function () {
          try {
            const url = String(this.__cs2thUrl || '');
            const txt = String(this.responseText || '');
            if (!txt || txt.length > 200000) return;
            if (!/Auth|SignIn|Login|Token|UserInfo|user\/Account/i.test(url) && !/"Token"\s*:/.test(txt)) return;
            const j = JSON.parse(txt);
            const t = digToken(j);
            if (t) capture(t, 'xhr-body:' + url);
          } catch (e) {}
        });
      } catch (e) {}
      return xs.apply(this, arguments);
    };
  } catch (e) {}
  try {
    const ofetch = window.fetch;
    if (typeof ofetch === 'function') {
      window.fetch = function (input, init) {
        try {
          const h = init && init.headers;
          if (h) {
            if (typeof Headers !== 'undefined' && h instanceof Headers) {
              capture(h.get('Authorization') || h.get('authorization'), 'fetch-header');
            } else if (Array.isArray(h)) {
              for (const pair of h) {
                if (pair && /^authorization$/i.test(String(pair[0] || ''))) capture(pair[1], 'fetch-header');
              }
            } else if (typeof h === 'object') {
              for (const [k, v] of Object.entries(h)) {
                if (/^authorization$/i.test(String(k || ''))) capture(v, 'fetch-header');
              }
            }
          }
        } catch (e) {}
        const p = ofetch.apply(this, arguments);
        try {
          p.then(function (res) {
            try {
              const url = String((res && res.url) || input || '');
              if (!/Auth|SignIn|Login|Token|UserInfo|user\/Account|api\.youpin898/i.test(url)) return res;
              return res.clone().text().then(function (txt) {
                try {
                  if (txt && /"Token"\s*:/.test(txt)) {
                    const j = JSON.parse(txt);
                    const t = digToken(j);
                    if (t) capture(t, 'fetch-body:' + url);
                  }
                } catch (e) {}
                return res;
              });
            } catch (e) { return res; }
          }).catch(function () {});
        } catch (e) {}
        return p;
      };
    }
  } catch (e) {}
  return true;
})()
"""

_YOUPIN_SCRAPE_JS = """() => {
  const read = store => {
    const out = {};
    try {
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i);
        out[key] = store.getItem(key);
      }
    } catch (e) {}
    return out;
  };
  return {
    hookedAuth: String(window.__cs2thYpAuth || ''),
    hookedSource: String(window.__cs2thYpAuthSource || ''),
    local: read(localStorage),
    session: read(sessionStorage),
  };
}"""

_STORAGE_READ_JS = """() => {
  const read = store => {
    const out = {};
    try {
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i);
        out[key] = store.getItem(key);
      }
    } catch (e) {}
    return out;
  };
  return { local: read(localStorage), session: read(sessionStorage) };
}"""

_C5_LOGIN_STATE_JS = """() => {
  const href = String(location.href || '');
  const path = String(location.pathname || '');
  const onLogin = /\\/login(\\/|$|\\?)/i.test(path) || /loginMiddle/i.test(path);
  const onBan = /console-ban/i.test(path);
  const onUser = /user-center|\\/user\\/user|\\/user\\/order|\\/user\\/sell/i.test(href);
  let nickname = '';
  try {
    const el = document.querySelector(
      '[class*="userName"], [class*="nickname"], [class*="user-name"], .user-name'
    );
    if (el && el.textContent) nickname = String(el.textContent).trim().slice(0, 64);
  } catch (e) {}
  // Only treat personal-center pages as logged-in; bare non-login tabs are not enough.
  return { onLogin, onBan, onUser, nickname, loggedIn: onUser && !onLogin && !onBan };
}"""

_ECO_LOGIN_STATE_JS = """() => {
  const href = String(location.href || '');
  const path = String(location.pathname || '');
  const onPerson = /\\/person|user-center|account|member/i.test(href + path);
  const body = String((document.body && document.body.innerText) || '').slice(0, 2000);
  const hasLoginCta = /立即登录|手机登录|扫码登录/.test(body);
  return {
    onPerson,
    nickname: '',
    hasLoginCta,
    loggedIn: onPerson || !hasLoginCta,
  };
}"""


def _read_page_storage(page) -> dict[str, Any]:
    try:
        raw = page.evaluate(_STORAGE_READ_JS)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _auth_cookie_present(cookie: str, keys: tuple[str, ...]) -> bool:
    lower = str(cookie or "").lower()
    return any(f"{key.lower()}=" in lower for key in keys)


def _close_context_quiet(context) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


class MarketplaceLoginValidationWorker(QThread):
    provider_checked = Signal(str, object)
    completed = Signal()

    def __init__(self, providers: list[str], parent=None) -> None:
        super().__init__(parent)
        self.providers = list(providers)

    def run(self) -> None:
        providers = list(dict.fromkeys(self.providers))
        if not providers:
            self.completed.emit()
            return

        def check_one(provider: str) -> tuple[str, dict[str, Any]]:
            result = validate_provider_login(provider, timeout=5.0)
            if not isinstance(result, dict):
                result = {
                    "provider": provider,
                    "ok": False,
                    "indeterminate": True,
                    "message": "校验返回格式异常",
                }
            return provider, result

        # Validate platforms concurrently so ECO's slow browser check does not
        # block BUFF / 悠悠 / C5 from finishing.
        with ThreadPoolExecutor(
            max_workers=min(4, len(providers)),
            thread_name_prefix="login-validate",
        ) as executor:
            futures = {
                executor.submit(check_one, provider): provider
                for provider in providers
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    checked_provider, result = future.result()
                except Exception as exc:  # noqa: BLE001 - surface per-provider failure
                    checked_provider = provider
                    result = {
                        "provider": provider,
                        "ok": False,
                        "indeterminate": True,
                        "message": f"校验异常：{exc}",
                    }
                self.provider_checked.emit(checked_provider, result)
        self.completed.emit()


def _validate_c5_eco_browser_session(provider: str) -> dict[str, Any]:
    """Confirm C5/ECO login by silently reading the APP browser profile."""
    name = provider_display_name_safe(provider)
    refreshed = _refresh_saved_browser_login(provider)
    if refreshed.get("refreshed"):
        return {
            "provider": provider,
            "ok": True,
            "browser_session": True,
            "message": f"{name} 登录有效（已静默读取浏览器会话）",
        }
    return {
        "provider": provider,
        "ok": False,
        "indeterminate": False,
        "message": refreshed.get("message")
        or f"{name} 未登录或会话已失效，请重新登录",
    }


def _refresh_saved_browser_login(provider: str) -> dict[str, Any]:
    """Silently reload C5/ECO credentials from the app-owned Chromium profile."""
    from core.market_external_browser import harvest_profile_cookies

    profile = Path(CACHE_DIR) / "market_browser_profiles" / provider
    if not profile.is_dir():
        return {"refreshed": False, "message": "尚无 APP 浏览器登录记录"}
    hints = ("c5game.com", "zbt.com") if provider == "c5" else ("ecosteam.cn",)
    if provider == "eco":
        context = None
        try:
            with sync_playwright() as playwright:
                context = launch_persistent_chromium_context(
                    playwright,
                    profile,
                    headless=True,
                    viewport=None,
                    webdriver_stealth=False,
                    market_browser=True,
                    isolated_profile=True,
                )
                page = focus_single_page(context)
                page.goto(_LOGIN_URLS["eco"], wait_until="commit", timeout=30_000)
                page.wait_for_timeout(800)
                cookie = _cookie_header(context.cookies(), *hints)
                storage = _read_page_storage(page)
                tokens = _tokens_from_storage(storage, loose=True)
                token = tokens[0] if tokens else ""
                has_refresh = _auth_cookie_present(cookie, ("refreshToken",))
                logged_in = False
                try:
                    state = page.evaluate(_ECO_LOGIN_STATE_JS)
                    if isinstance(state, dict) and state.get("loggedIn"):
                        logged_in = True
                except Exception:
                    pass
                has_cred = (
                    has_refresh
                    or bool(token)
                    or _auth_cookie_present(
                        cookie,
                        (
                            "token",
                            "authorization",
                            "eco_token",
                            "access_token",
                            "auth",
                            "refreshToken",
                        ),
                    )
                )
                if not (has_cred and (logged_in or has_refresh)):
                    return {
                        "refreshed": False,
                        "message": "未发现有效 ECOSteam 登录会话",
                    }
        except Exception as exc:  # noqa: BLE001
            return {"refreshed": False, "message": str(exc)}
        finally:
            _close_context_quiet(context)
        saved = save_eco_auth(token, cookie)
        if not saved.get("ok"):
            return {
                "refreshed": False,
                "message": "未发现有效 ECOSteam 登录 Token / Cookie",
            }
        return {"refreshed": True, "message": ""}
    try:
        cookies = harvest_profile_cookies(profile, domain_hints=hints)
    except Exception as exc:  # noqa: BLE001 - returned as an indeterminate hint
        return {"refreshed": False, "message": str(exc)}
    cookie = _cookie_header(cookies, *hints)
    if provider == "c5":
        auth_names = {
            "nc5_accesstoken",
            "c5token",
            "access_token",
            "ncaccess",
            "token",
            "authorization",
        }
        token = ""
        for item in cookies:
            name = str(item.get("name") or "").strip().lower()
            value = str(item.get("value") or "").strip()
            if name in auth_names and len(value) > len(token):
                token = value
        if not token and not _auth_cookie_present(cookie, tuple(auth_names)):
            return {"refreshed": False, "message": "未发现有效 C5GAME 登录 Cookie"}
        saved = save_c5_auth(cookie, token)
        if not saved.get("ok"):
            return {
                "refreshed": False,
                "message": "未发现有效 C5GAME 登录 Cookie",
            }
        return {"refreshed": True, "message": ""}
    return {"refreshed": False, "message": "不支持的平台"}


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
        emitted = False
        try:
            self.progress.emit(provider, "正在启动登录窗口…")
            # Collection may still hold the same profile via an access window.
            try:
                from core.market_access_session import close_access_sessions

                close_access_sessions(provider)
            except Exception:
                pass
            # C5 bans Playwright CDP as「开发者模式」— use a normal system browser.
            if provider == "c5":
                result = self._capture_c5_external()
                self.completed.emit(provider, result)
                emitted = True
                return
            with sync_playwright() as playwright:
                profile = (
                    Path(CACHE_DIR)
                    / "market_browser_profiles"
                    / provider
                )
                profile.mkdir(parents=True, exist_ok=True)
                use_market = provider == "eco"
                context = launch_persistent_chromium_context(
                    playwright,
                    profile,
                    headless=False,
                    viewport=None if use_market else (1280, 840),
                    webdriver_stealth=False,
                    market_browser=use_market,
                    isolated_profile=True,
                )
                page = focus_single_page(context)
                self.progress.emit(
                    provider,
                    f"请在弹出窗口完成 {provider_display_name_safe(provider)} 登录…",
                )
                if provider == "buff":
                    result = self._capture_buff(context, page)
                elif provider == "yyyp":
                    result = self._capture_youpin(context, page)
                else:
                    result = self._capture_eco(context, page)
                # Notify UI before tearing down Chromium — close() can take seconds.
                self.completed.emit(provider, result)
                emitted = True
        except (SteamBrowserNotFoundError, SteamBrowserLaunchError) as exc:
            result = {
                "ok": False,
                "indeterminate": True,
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - surface browser/runtime failures
            result = {
                "ok": False,
                "indeterminate": True,
                "message": f"登录窗口启动或捕获失败：{exc}",
            }
        finally:
            _close_context_quiet(context)
        if not emitted:
            self.completed.emit(provider, result)


def provider_display_name_safe(provider: str) -> str:
    try:
        from core.market_candidates import provider_display_name

        return provider_display_name(provider)
    except Exception:
        return provider


def _capture_c5_external(self) -> dict[str, Any]:
    """Open system Chrome/Edge for C5 login; harvest cookies after the window closes."""
    from core.market_candidates import clear_c5_session_auth
    from core.market_external_browser import (
        c5_netlog_login_ready,
        harvest_c5_netlog_headers,
        harvest_profile_cookies,
        launch_system_browser,
        wait_browser_closed,
    )

    clear_c5_session_auth()
    profile = Path(CACHE_DIR) / "market_browser_profiles" / "c5"
    profile.mkdir(parents=True, exist_ok=True)
    net_log = profile.parent / "c5-login-netlog.json"
    try:
        net_log.unlink(missing_ok=True)
    except OSError:
        pass
    self.progress.emit(
        "c5",
        "已打开系统浏览器，请完成登录后关闭该浏览器窗口…",
    )
    try:
        proc = launch_system_browser(
            profile_dir=profile,
            url=_LOGIN_URLS["c5"],
            net_log_path=net_log,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            net_log.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "ok": False,
            "indeterminate": True,
            "message": str(exc),
        }
    closed = wait_browser_closed(
        proc,
        timeout_s=420.0,
        progress=lambda message: self.progress.emit("c5", message),
        progress_message=(
            "请在 C5 页面完成登录或安全验证；成功后助手会自动关闭窗口…"
        ),
        auto_close_when=lambda: c5_netlog_login_ready(net_log),
        auto_close_message="已检测到 C5GAME 登录成功，正在自动关闭登录窗口…",
    )
    if not closed:
        try:
            net_log.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "ok": False,
            "message": "等待登录超时：请关闭浏览器窗口后重试",
        }
    # Give Chromium a moment to flush cookies to disk.
    time.sleep(1.2)
    self.progress.emit("c5", "正在读取登录凭证…")
    try:
        cookies = harvest_profile_cookies(
            profile,
            domain_hints=("c5game.com", "zbt.com"),
        )
    except Exception as exc:  # noqa: BLE001
        try:
            net_log.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "ok": False,
            "indeterminate": True,
            "message": str(exc),
        }
    cookie = _cookie_header(cookies, "c5game.com", "zbt.com")
    try:
        client_headers = harvest_c5_netlog_headers(net_log)
        if client_headers:
            save_c5_client_headers(client_headers)
    finally:
        try:
            net_log.unlink(missing_ok=True)
        except OSError:
            pass
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
        }:
            if len(value) > len(token):
                token = value
    if not cookie and not token:
        return {
            "ok": False,
            "message": "未捕获到有效 C5GAME 登录凭证，请重新打开登录窗口并完成登录",
        }
    self.progress.emit("c5", "已捕获登录状态，正在快速确认…")
    return _finish_c5_login(cookie, token, "")


def _finish_c5_login(
    cookie: str,
    token: str,
    nickname: str = "",
) -> dict[str, Any]:
    verified = validate_c5_credentials(cookie, token, timeout=12.0, quick=False)
    if not verified.get("ok"):
        return verified
    save_c5_auth(
        cookie,
        token,
        nickname=str(verified.get("account_name") or nickname),
        user_id=verified.get("user_id"),
    )
    return verified


def _finish_eco_login(
    token: str,
    cookie: str,
    nickname: str = "",
) -> dict[str, Any]:
    verified = validate_eco_credentials(token, cookie, timeout=3.0, quick=True)
    if verified.get("ok"):
        save_eco_auth(
            token,
            cookie,
            nickname=str(verified.get("account_name") or nickname),
            user_id=verified.get("user_id"),
        )
        return verified
    saved = save_eco_auth(token, cookie, nickname=nickname, user_id=None)
    if saved.get("ok"):
        return {
            "ok": True,
            "provider": "eco",
            "message": "ECOSteam 登录凭证已保存",
            "account_name": nickname,
        }
    return {
        "ok": False,
        "message": "未捕获到有效 ECOSteam 登录凭证，请重新打开登录窗口",
    }


def _open_eco_login_layer(page) -> bool:
    """Open ECO's homepage login/QR modal (site has no standalone login URL)."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    # Already signed-in chrome: do not force the modal.
    try:
        if page.locator(".nav-logining:not(.hide)").count() > 0:
            return False
    except Exception:
        pass
    try:
        btn = page.locator(".loginBtn").first
        btn.wait_for(state="visible", timeout=8_000)
        btn.click(timeout=5_000)
        return True
    except Exception:
        pass
    # Fallback when the header button is hidden but the plugin is loaded.
    try:
        return bool(
            page.evaluate(
                """() => {
                  try {
                    if (window.jQuery && jQuery.fn && jQuery.fn.showLoginLayer) {
                      jQuery(document.body).showLoginLayer({ showLogin: true });
                      return true;
                    }
                  } catch (e) {}
                  const btn = document.querySelector('.loginBtn');
                  if (btn) { btn.click(); return true; }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


# Attach optimized capture helpers to the worker class.
def _capture_buff(self, context, page) -> dict[str, Any]:
    page.goto(_LOGIN_URLS["buff"], wait_until="commit", timeout=45_000)
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            header = _cookie_header(context.cookies(), "buff.163.com")
            if "session=" in header.lower():
                save_buff_auth(header)
                self.progress.emit("buff", "已捕获 BUFF Cookie，正在校验…")
                return validate_provider_login("buff", timeout=4.0)
            if not context.pages:
                break
            page.wait_for_timeout(600)
        except Exception:
            if not getattr(context, "pages", []):
                break
            time.sleep(0.6)
    return {
        "ok": False,
        "message": "未捕获到 BUFF 登录 Cookie，请重新打开登录窗口",
    }


def _capture_youpin(self, context, page) -> dict[str, Any]:
    captured: list[str] = []
    tried: set[str] = set()
    last_scan = 0.0

    def push_token(raw: Any) -> None:
        token = _normalized_token(raw)
        if token and token not in captured and token not in tried:
            captured.append(token)

    def inspect_request(request) -> None:
        try:
            headers = request.headers
            push_token(headers.get("authorization") or headers.get("Authorization"))
        except Exception:
            pass

    def inspect_response(response) -> None:
        try:
            url = str(response.url or "")
            if "youpin898" not in url.lower():
                return
            if not re.search(
                r"Auth|SignIn|Login|Token|UserInfo|Account",
                url,
                flags=re.I,
            ):
                return
            body = response.text()
            if not body or len(body) > 200_000:
                return
            if '"Token"' not in body and '"token"' not in body:
                return
            payload = json.loads(body)
            data = payload.get("Data") or payload.get("data") or payload
            if isinstance(data, dict):
                for key in (
                    "Token",
                    "token",
                    "accessToken",
                    "AccessToken",
                    "authorization",
                    "Authorization",
                ):
                    push_token(data.get(key))
        except Exception:
            pass

    def ensure_hook(target) -> None:
        try:
            target.evaluate(_YOUPIN_HOOK_JS)
        except Exception:
            pass

    context.on("request", inspect_request)
    context.on("response", inspect_response)
    try:
        context.add_init_script(_YOUPIN_HOOK_JS)
    except Exception:
        pass
    # Stay on the homepage. Do NOT bounce to /market/goods-list without
    # templateId — Youpin treats that as「参数错误」and breaks the login UI.
    page.goto(_LOGIN_URLS["yyyp"], wait_until="commit", timeout=45_000)
    ensure_hook(page)
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            cookie = _cookie_header(context.cookies(), "youpin898.com")
            now = time.monotonic()
            # Full storage scrape is expensive; only do it every ~1.5s.
            if now - last_scan >= 1.5:
                last_scan = now
                for current_page in list(context.pages):
                    try:
                        ensure_hook(current_page)
                        storage = current_page.evaluate(_YOUPIN_SCRAPE_JS)
                        if not isinstance(storage, dict):
                            continue
                        push_token(storage.get("hookedAuth"))
                        for token in _tokens_from_storage(
                            {
                                "local": storage.get("local") or {},
                                "session": storage.get("session") or {},
                            }
                        ):
                            push_token(token)
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
                    timeout=4,
                )
                if verified.get("ok"):
                    save_youpin_auth(
                        token,
                        cookie,
                        nickname=str(verified.get("account_name") or ""),
                        user_id=verified.get("user_id"),
                    )
                    return verified
            if not context.pages:
                break
            page.wait_for_timeout(500)
        except Exception:
            if not getattr(context, "pages", []):
                break
            time.sleep(0.5)
    return {
        "ok": False,
        "message": "未捕获到有效悠悠有品 Token，请重新打开登录窗口",
    }


def _capture_c5(self, context, page) -> dict[str, Any]:
    captured: list[str] = []
    client_headers: dict[str, str] = {}
    last_cookie = ""
    last_url = ""
    last_heavy_scan = 0.0
    ban_warned = False
    login_url = _LOGIN_URLS["c5"]

    def inspect_request(request) -> None:
        try:
            headers = request.headers
            for key in (
                "authorization",
                "Authorization",
                "x-access-token",
                "X-Access-Token",
                "access-token",
                "Access-Token",
            ):
                token = _loose_token(headers.get(key))
                if token and token not in captured:
                    captured.append(token)
            url = str(getattr(request, "url", "") or "")
            if "c5game.com" in url or "zbt.com" in url:
                for key in (
                    "App-Version",
                    "app-version",
                    "platform",
                    "User-Agent",
                    "user-agent",
                    "x-area",
                    "x-traffic-tag",
                ):
                    value = headers.get(key)
                    if value:
                        client_headers[key] = str(value)
        except Exception:
            pass

    def on_new_page(_new_page) -> None:
        # Keep the original login tab; close restored/duplicate C5 tabs.
        nonlocal page
        try:
            keep = page if page in list(context.pages) else _new_page
            page = focus_single_page(context, preferred=keep)
        except Exception:
            pass

    context.on("request", inspect_request)
    context.on("page", on_new_page)
    page = focus_single_page(context, preferred=page)
    page.goto(login_url, wait_until="commit", timeout=45_000)
    page = focus_single_page(context, preferred=page)
    deadline = time.monotonic() + 420
    left_login_since = 0.0
    while time.monotonic() < deadline:
        try:
            page = focus_single_page(context, preferred=page)
            cookie = _cookie_header(
                context.cookies(),
                "c5game.com",
                "zbt.com",
            )
            try:
                href = str(page.url or "")
            except Exception:
                href = ""
            now = time.monotonic()
            on_ban = "console-ban" in href.lower()
            if on_ban and not ban_warned:
                ban_warned = True
                self.progress.emit(
                    "c5",
                    "C5 拦截页：请勿按 F12；在页面点「重新加载」后继续登录",
                )
            cookie_changed = cookie != last_cookie
            url_changed = href != last_url
            last_cookie = cookie
            last_url = href
            left_login = not re.search(
                r"/login(/|$|\?)|loginMiddle|console-ban",
                href,
                flags=re.I,
            )
            if left_login:
                if not left_login_since:
                    left_login_since = time.monotonic()
            else:
                left_login_since = 0.0
            has_cred = bool(captured) or _auth_cookie_present(
                cookie,
                ("c5token", "access_token", "ncaccess", "token", "authorization"),
            )
            logged_in = False
            nickname = ""
            # Avoid page.evaluate on console-ban (CDP looks like DevTools to C5).
            if (
                not on_ban
                and (cookie_changed or url_changed or has_cred)
                and (now - last_heavy_scan >= 1.2)
            ):
                last_heavy_scan = now
                try:
                    if not captured:
                        storage = _read_page_storage(page)
                        for token in _tokens_from_storage(storage, loose=True):
                            if token not in captured:
                                captured.append(token)
                    state = page.evaluate(_C5_LOGIN_STATE_JS)
                    if isinstance(state, dict):
                        if state.get("loggedIn"):
                            logged_in = True
                        nick = str(state.get("nickname") or "").strip()
                        if nick:
                            nickname = nick
                except Exception:
                    pass
            # Require personal-center marker, or stable post-login URL + creds.
            stable_home = bool(left_login_since and now - left_login_since >= 1.5)
            if has_cred and (logged_in or (stable_home and (captured or has_cred))):
                token = captured[0] if captured else ""
                self.progress.emit("c5", "已捕获登录状态，正在快速确认…")
                if client_headers:
                    try:
                        from core.market_candidates import save_c5_client_headers

                        save_c5_client_headers(client_headers)
                    except Exception:
                        pass
                return _finish_c5_login(cookie, token, nickname)
            if not context.pages:
                break
            page.wait_for_timeout(500)
        except Exception:
            if not getattr(context, "pages", []):
                break
            time.sleep(0.5)
    return {
        "ok": False,
        "message": "未捕获到有效 C5GAME 登录凭证，请重新打开登录窗口",
    }


def _capture_eco(self, context, page) -> dict[str, Any]:
    captured: list[str] = []
    last_cookie = ""
    last_heavy_scan = 0.0

    def inspect_request(request) -> None:
        try:
            headers = request.headers
            for key in (
                "authorization",
                "Authorization",
                "x-access-token",
                "X-Access-Token",
                "access-token",
                "Access-Token",
                "token",
                "Token",
            ):
                token = _loose_token(headers.get(key))
                if token and token not in captured:
                    captured.append(token)
        except Exception:
            pass

    def on_new_page(_new_page) -> None:
        nonlocal page
        try:
            keep = page if page in list(context.pages) else _new_page
            page = focus_single_page(context, preferred=keep)
        except Exception:
            pass

    context.on("request", inspect_request)
    context.on("page", on_new_page)
    page = focus_single_page(context, preferred=page)
    page.goto(_LOGIN_URLS["eco"], wait_until="domcontentloaded", timeout=45_000)
    page = focus_single_page(context, preferred=page)
    if _open_eco_login_layer(page):
        self.progress.emit("eco", "已打开登录弹层，请扫码或手机登录…")
    else:
        self.progress.emit(
            "eco",
            "请在弹出窗口完成 ECOSteam 登录（可点右上角「登录/注册」）…",
        )
    try:
        last_cookie = _cookie_header(context.cookies(), "ecosteam.cn")
    except Exception:
        last_cookie = ""
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            page = focus_single_page(context, preferred=page)
            cookie = _cookie_header(context.cookies(), "ecosteam.cn")
            cookie_tick = cookie != last_cookie
            last_cookie = cookie
            has_refresh_cookie = _auth_cookie_present(
                cookie,
                ("refreshToken",),
            )
            has_cred = has_refresh_cookie or bool(captured) or _auth_cookie_present(
                cookie,
                (
                    "token",
                    "authorization",
                    "eco_token",
                    "access_token",
                    "auth",
                    "refreshToken",
                ),
            )
            logged_in = False
            nickname = ""
            now = time.monotonic()
            if (cookie_tick or has_cred) and (now - last_heavy_scan >= 1.2):
                last_heavy_scan = now
                try:
                    if not captured:
                        storage = _read_page_storage(page)
                        for token in _tokens_from_storage(storage, loose=True):
                            if token not in captured:
                                captured.append(token)
                    state = page.evaluate(_ECO_LOGIN_STATE_JS)
                    if isinstance(state, dict) and state.get("loggedIn"):
                        logged_in = True
                except Exception:
                    pass
            # Cookie changes also happen for ECO's risk-control/CDN cookies and
            # previously caused false "login succeeded" results.  The website's
            # wear filter explicitly requires refreshToken; otherwise require
            # the page itself to report a logged-in account.
            if has_cred and (logged_in or has_refresh_cookie):
                token = captured[0] if captured else ""
                self.progress.emit("eco", "已捕获登录状态，正在快速确认…")
                return _finish_eco_login(token, cookie, nickname)
            if not context.pages:
                break
            page.wait_for_timeout(500)
        except Exception:
            if not getattr(context, "pages", []):
                break
            time.sleep(0.5)
    return {
        "ok": False,
        "message": "未捕获到有效 ECOSteam 登录凭证，请重新打开登录窗口",
    }


MarketplaceLoginCaptureWorker._capture_buff = _capture_buff  # type: ignore[method-assign]
MarketplaceLoginCaptureWorker._capture_youpin = _capture_youpin  # type: ignore[method-assign]
MarketplaceLoginCaptureWorker._capture_c5 = _capture_c5  # type: ignore[method-assign]
MarketplaceLoginCaptureWorker._capture_c5_external = _capture_c5_external  # type: ignore[method-assign]
MarketplaceLoginCaptureWorker._capture_eco = _capture_eco  # type: ignore[method-assign]
