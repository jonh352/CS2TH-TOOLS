"""C5 exact-wear collection via a hidden system Chrome/Edge window.

Uses a real (non-headless) Chromium + CDP to load sell pages and sniff
``/search/v2/sell/{id}/list``. The native window stays hidden during collection
and is restored only when C5 requires user verification.
After the first successful sniff, later materials reuse captured request
headers for page 1 (and later pages) via HTTP, falling back to navigation only
when those headers stop working.
All Playwright Sync API calls run on a dedicated worker thread so they never
collide with an asyncio loop on the collection thread.
Session is reused for one C5 wave. A verification window is left open for the
user; ordinary collection windows are closed immediately with the wave.
"""

from __future__ import annotations

import logging
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from config import CACHE_DIR
from core.collection_cancel import CancelCheck, CollectionCancelled, raise_if_cancelled
from core.market_external_browser import (
    harvest_profile_cookies,
    resolve_system_browser_executable,
)
from core.steam.launch import (
    _clear_stale_profile_singletons,
    clear_chromium_session_restore,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str], None] | None

_C5_SELL_LIST_PAGE_SIZE = 40
_C5_SELL_LIST_ORDER_BY_PRICE_ASC = 2
_LIST_URL_MARK = "/search/v2/sell/"
_TLS = threading.local()


def _login_profile() -> Path:
    path = CACHE_DIR / "market_browser_profiles" / "c5"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _collect_profile() -> Path:
    """Separate profile so login windows and collect CDP do not lock each other."""
    path = CACHE_DIR / "market_browser_profiles" / "c5_collect"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hidden_browser_startupinfo() -> subprocess.STARTUPINFO | None:
    """Ask Windows to keep Chromium's first native window hidden at creation."""
    import os

    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return startupinfo


def _wait_for_cdp(
    port: int,
    proc: subprocess.Popen,
    *,
    cancel_check: CancelCheck = None,
    timeout_s: float = 20.0,
) -> None:
    """Wait until Chromium's DevTools HTTP endpoint accepts connections."""
    from core.market_candidates import C5PlatformPausedError

    url = f"http://127.0.0.1:{int(port)}/json/version"
    deadline = time.monotonic() + max(5.0, float(timeout_s))
    last_error = ""
    while time.monotonic() < deadline:
        raise_if_cancelled(cancel_check)
        _hide_browser_windows(proc)
        if proc.poll() is not None:
            raise C5PlatformPausedError(
                "C5GAME 采集浏览器已退出，无法建立通道"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.6) as response:
                if int(getattr(response, "status", 200) or 200) == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.12)
    detail = f"：{last_error}" if last_error else ""
    raise C5PlatformPausedError(f"C5GAME 采集通道未就绪{detail}")


def _descendant_pids(root_pid: int) -> set[int]:
    """Return ``root_pid`` plus children (Chrome/Edge often own the HWND elsewhere)."""
    import ctypes
    import os
    from ctypes import wintypes

    pids = {int(root_pid)}
    if os.name != "nt" or root_pid <= 0:
        return pids

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if snapshot in (0, invalid_handle, None):
        return pids
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return pids
        children: dict[int, list[int]] = {}
        while True:
            parent = int(entry.th32ParentProcessID)
            child = int(entry.th32ProcessID)
            children.setdefault(parent, []).append(child)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        stack = [int(root_pid)]
        while stack:
            current = stack.pop()
            for child in children.get(current, []):
                if child not in pids:
                    pids.add(child)
                    stack.append(child)
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def _hide_browser_windows(proc: subprocess.Popen | None) -> None:
    """Hide top-level windows for ``proc`` (and child PIDs) from the taskbar."""
    import ctypes
    import os

    if os.name != "nt" or proc is None or proc.poll() is not None:
        return
    target_pids = _descendant_pids(int(proc.pid))
    user32 = ctypes.windll.user32
    sw_hide = 0
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value in target_pids and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, sw_hide)
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        pass


def _show_browser_windows(proc: subprocess.Popen | None) -> None:
    """Restore and foreground C5's native window for user verification."""
    import ctypes
    import os

    if os.name != "nt" or proc is None or proc.poll() is not None:
        return
    target_pids = _descendant_pids(int(proc.pid))
    user32 = ctypes.windll.user32
    sw_restore = 9
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        is_main_window = (
            process_id.value in target_pids
            and user32.GetWindow(hwnd, 4) == 0  # GW_OWNER
            and user32.GetWindowTextLengthW(hwnd) > 0
        )
        if is_main_window:
            user32.ShowWindow(hwnd, sw_restore)
            user32.SetForegroundWindow(hwnd)
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        pass


def _cookies_from_header(cookie: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in str(cookie or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            continue
        out.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".c5game.com",
                "path": "/",
            }
        )
    return out


def _merge_cookie_lists(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = item.get("value")
            if not name or value is None:
                continue
            shaped = {
                "name": name,
                "value": str(value),
                "domain": str(item.get("domain") or ".c5game.com"),
                "path": str(item.get("path") or "/"),
            }
            expires = item.get("expires")
            if expires is not None:
                try:
                    shaped["expires"] = float(expires)
                except (TypeError, ValueError):
                    pass
            by_name[name.lower()] = shaped
    return list(by_name.values())


def _is_sell_list_url(url: str, item_id: int) -> bool:
    text = str(url or "")
    return f"{_LIST_URL_MARK}{item_id}/list" in text


def _risk_text(payload: dict[str, Any] | None, extra: str = "") -> str:
    parts = [extra]
    if isinstance(payload, dict):
        parts.append(str(payload.get("errorMsg") or ""))
        parts.append(str(payload.get("msg") or ""))
        parts.append(str(payload.get("errorCode") or ""))
    return " ".join(parts)


def _looks_like_risk(text: str) -> bool:
    raw = str(text or "")
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
            "console-ban",
            "captcha",
            "slider",
            "访问校验",
            "请完成验证",
        )
    )


class C5BrowserCollector:
    """One hidden system-browser CDP session reused across C5 materials.

    Playwright Sync API is confined to an internal worker thread.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._headers: dict[str, str] | None = None
        self._opened = False
        self._verification_visible = False
        self._cmd_q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="c5-browser-cdp",
            daemon=True,
        )
        self._worker_stopped = threading.Event()
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._cmd_q.get()
            if item is None:
                try:
                    self._close_impl()
                finally:
                    self._worker_stopped.set()
                return
            op, kwargs, result_box, done = item
            try:
                if op == "ensure_open":
                    self._ensure_open_impl(**kwargs)
                    result_box["ok"] = True
                elif op == "fetch":
                    result_box["ok"] = True
                    result_box["value"] = self._fetch_list_payload_impl(**kwargs)
                elif op == "close":
                    self._close_impl()
                    result_box["ok"] = True
                else:
                    raise RuntimeError(f"unknown C5 browser op: {op}")
            except BaseException as exc:  # noqa: BLE001 - marshal to caller
                result_box["ok"] = False
                result_box["exc"] = exc
            finally:
                done.set()

    def _invoke(
        self,
        op: str,
        kwargs: dict[str, Any],
        *,
        cancel_check: CancelCheck = None,
    ) -> Any:
        if not self._worker.is_alive():
            from core.market_candidates import C5PlatformPausedError

            raise C5PlatformPausedError("C5GAME 采集通道已停止")
        done = threading.Event()
        result_box: dict[str, Any] = {}
        self._cmd_q.put((op, kwargs, result_box, done))
        while not done.wait(0.1):
            raise_if_cancelled(cancel_check)
        if not result_box.get("ok"):
            exc = result_box.get("exc")
            if isinstance(exc, BaseException):
                raise exc
            raise RuntimeError("C5GAME 采集通道内部错误")
        return result_box.get("value")

    def ensure_open(
        self,
        *,
        progress: ProgressCb = None,
        cancel_check: CancelCheck = None,
    ) -> None:
        raise_if_cancelled(cancel_check)
        self._invoke(
            "ensure_open",
            {"progress": progress, "cancel_check": cancel_check},
            cancel_check=cancel_check,
        )

    def fetch_list_payload(
        self,
        *,
        item_id: int,
        min_wear: float,
        max_wear: float,
        page_no: int = 1,
        display_name: str = "",
        progress: ProgressCb = None,
        cancel_check: CancelCheck = None,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        raise_if_cancelled(cancel_check)
        return self._invoke(
            "fetch",
            {
                "item_id": item_id,
                "min_wear": min_wear,
                "max_wear": max_wear,
                "page_no": page_no,
                "display_name": display_name,
                "progress": progress,
                "cancel_check": cancel_check,
                "timeout_s": timeout_s,
            },
            cancel_check=cancel_check,
        )

    def close(self) -> None:
        if self._worker.is_alive():
            done = threading.Event()
            result_box: dict[str, Any] = {}
            self._cmd_q.put(("close", {}, result_box, done))
            done.wait(timeout=20.0)
            self._cmd_q.put(None)
            self._worker.join(timeout=10.0)
        else:
            self._close_impl()

    def _ensure_open_impl(
        self,
        *,
        progress: ProgressCb = None,
        cancel_check: CancelCheck = None,
    ) -> None:
        if self._opened:
            return
        from core.market_candidates import (
            C5PlatformPausedError,
            load_c5_auth_for_browser,
        )

        raise_if_cancelled(cancel_check)
        exe = resolve_system_browser_executable()
        if exe is None:
            raise C5PlatformPausedError(
                "未找到本机 Chrome / Edge，无法继续 C5GAME 采集"
            )
        if progress:
            progress("C5GAME · 正在准备采集…")

        profile = _collect_profile()
        clear_chromium_session_restore(profile)
        _clear_stale_profile_singletons(profile)
        port = _pick_free_port()
        try:
            # Keep a real headed browser to avoid changing C5's browser fingerprint.
            self._proc = subprocess.Popen(
                [
                    str(exe),
                    f"--user-data-dir={profile}",
                    f"--remote-debugging-port={port}",
                    "--remote-allow-origins=*",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--start-minimized",
                    # Size only matters if the user restores the taskbar icon.
                    "--window-size=900,700",
                    "https://www.c5game.com/csgo",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=_hidden_browser_startupinfo(),
            )
            _hide_browser_windows(self._proc)
            _wait_for_cdp(port, self._proc, cancel_check=cancel_check)

            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            last_connect_error: Exception | None = None
            for attempt in range(1, 6):
                raise_if_cancelled(cancel_check)
                _hide_browser_windows(self._proc)
                try:
                    self._browser = self._playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}"
                    )
                    last_connect_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_connect_error = exc
                    logger.info(
                        "C5 CDP connect attempt %s failed: %s", attempt, exc
                    )
                    if self._proc.poll() is not None:
                        break
                    time.sleep(0.25 * attempt)
            if self._browser is None:
                raise C5PlatformPausedError(
                    f"C5GAME 采集通道连接失败：{last_connect_error or 'unknown'}"
                )

            self._context = self._browser.contexts[0]
            # CDP attach often leaves several about:blank tabs — keep one and use it.
            pages = [p for p in list(self._context.pages) if p is not None]
            self._page = pages[0] if pages else self._context.new_page()
            for extra in pages[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            try:
                self._page.goto(
                    "https://www.c5game.com/csgo",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("C5 采集首页打开失败：%s", exc)
            _hide_browser_windows(self._proc)

            cookie_header, _token = load_c5_auth_for_browser()
            harvested: list[dict[str, Any]] = []
            try:
                harvested = harvest_profile_cookies(
                    _login_profile(),
                    domain_hints=("c5game.com", "zbt.com"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("C5 采集读取登录配置 Cookie 失败：%s", exc)
            cookies = _merge_cookie_lists(
                harvested,
                _cookies_from_header(cookie_header),
            )
            if cookies:
                try:
                    self._context.add_cookies(cookies)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("C5 采集注入 Cookie 失败：%s", exc)
            if not cookies:
                raise C5PlatformPausedError(
                    "C5GAME 无可用登录 Cookie，请先完成「登录 / 打开」"
                )
            # Prefer previously sniffed client headers so later materials can
            # skip sell-page navigation when the list API still accepts them.
            if not self._headers:
                try:
                    from core.market_candidates import _c5_client_headers

                    saved = _c5_client_headers()
                    if saved:
                        self._headers = dict(saved)
                except Exception:
                    pass
            self._opened = True
            if progress:
                progress("C5GAME · 采集通道已就绪")
        except CollectionCancelled:
            self._close_impl()
            raise
        except C5PlatformPausedError:
            self._close_impl()
            raise
        except Exception as exc:  # noqa: BLE001
            self._close_impl()
            raise C5PlatformPausedError(
                f"C5GAME 采集通道启动失败：{exc}"
            ) from exc

    def _close_impl(self) -> None:
        self._opened = False
        self._headers = None
        page, browser, playwright, proc = (
            self._page,
            self._browser,
            self._playwright,
            self._proc,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._proc = None
        preserve_verification_window = self._verification_visible
        self._verification_visible = False
        if preserve_verification_window:
            # Leave the real browser open so the user can finish C5's challenge.
            # Stopping Playwright only detaches automation; it does not make this
            # user-facing verification window headless or replace it with HTTP.
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass
            return
        for closer in (
            lambda: page.close() if page is not None else None,
            lambda: browser.close() if browser is not None else None,
            lambda: playwright.stop() if playwright is not None else None,
        ):
            try:
                closer()
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _fetch_list_payload_impl(
        self,
        *,
        item_id: int,
        min_wear: float,
        max_wear: float,
        page_no: int = 1,
        display_name: str = "",
        progress: ProgressCb = None,
        cancel_check: CancelCheck = None,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        from core.market_candidates import C5AccessGateError

        def raise_for_verification(message: str) -> None:
            self._verification_visible = True
            _show_browser_windows(self._proc)
            raise C5AccessGateError(message, needs_verify=True)

        raise_if_cancelled(cancel_check)
        self._ensure_open_impl(progress=progress, cancel_check=cancel_check)
        assert self._page is not None and self._context is not None
        label = display_name or "材料"
        page_no = max(1, int(page_no))
        list_url = (
            f"https://www.c5game.com/api/v1/search/v2/sell/{int(item_id)}/list"
            f"?itemId={int(item_id)}"
            f"&page={page_no}"
            f"&limit={_C5_SELL_LIST_PAGE_SIZE}"
            f"&orderBy={_C5_SELL_LIST_ORDER_BY_PRICE_ASC}"
            f"&minWear={float(min_wear):.8f}"
            f"&maxWear={float(max_wear):.8f}"
        )
        sell_page = (
            f"https://www.c5game.com/csgo/{int(item_id)}/item/sell"
            f"?minWear={float(min_wear):.8f}&maxWear={float(max_wear):.8f}"
        )

        def fetch_with_headers(*, announce: bool) -> dict[str, Any] | None:
            """Try list API with sniffed headers. None = soft fail → re-sniff."""
            if not self._headers:
                return None
            if announce and progress:
                if page_no > 1:
                    progress(f"C5GAME · {label} · 第 {page_no} 页")
                else:
                    progress(f"C5GAME · {label} · 拉取挂单…")
            try:
                response = self._context.request.get(
                    list_url,
                    headers=dict(self._headers),
                    timeout=30_000,
                )
            except CollectionCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("C5 list via headers failed: %s", exc)
                return None
            raise_if_cancelled(cancel_check)
            if response.status == 429:
                raise C5AccessGateError(
                    "C5GAME 返回访问频率过高",
                    needs_verify=False,
                )
            if int(response.status or 0) != 200:
                logger.info(
                    "C5 list via headers status=%s; will re-sniff",
                    response.status,
                )
                return None
            try:
                payload = response.json()
            except Exception:
                return None
            if not isinstance(payload, dict):
                return None
            risk = _risk_text(payload)
            if _looks_like_risk(risk):
                raise_for_verification(
                    f"C5GAME 需要安全验证：{risk.strip() or '风控'}"
                )
            return payload

        # Same session: reuse sniffed headers for later materials' page 1 as well.
        reused = fetch_with_headers(announce=True)
        if reused is not None:
            return reused
        if self._headers:
            logger.info(
                "C5 list headers unusable for itemId=%s page=%s; re-sniff sell page",
                item_id,
                page_no,
            )
            self._headers = None

        if progress:
            progress(f"C5GAME · {label} · 拉取挂单…")
        latest: dict[str, Any] | None = None
        captured_headers: dict[str, str] = {}

        def on_request(request: Any) -> None:
            if not _is_sell_list_url(request.url, int(item_id)):
                return
            if captured_headers:
                return
            try:
                raw = dict(request.headers)
            except Exception:
                return
            skip = {
                "host",
                "content-length",
                "connection",
                ":authority",
                ":method",
                ":path",
                ":scheme",
            }
            for key, value in raw.items():
                low = str(key).lower()
                if low in skip or low.startswith(":"):
                    continue
                captured_headers[str(key)] = str(value)

        def on_response(response: Any) -> None:
            nonlocal latest
            if not _is_sell_list_url(response.url, int(item_id)):
                return
            try:
                if str(response.request.method or "").upper() != "GET":
                    return
            except Exception:
                pass
            try:
                data = response.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            latest = data

        # Navigate once to sniff list XHR headers when reuse is unavailable.
        page = self._page
        try:
            pages = [p for p in list(self._context.pages) if p is not None]
            if page not in pages and pages:
                page = pages[0]
                self._page = page
            for extra in pages:
                if extra is not page:
                    try:
                        extra.close()
                    except Exception:
                        pass
        except Exception:
            pass
        page.on("request", on_request)
        page.on("response", on_response)
        try:
            # Navigation can recreate/show the native window, so hide it again.
            _hide_browser_windows(self._proc)
            page.goto(sell_page, wait_until="domcontentloaded", timeout=60_000)
            _hide_browser_windows(self._proc)
            href = str(page.url or "")
            if "console-ban" in href.lower():
                raise_for_verification("C5GAME 触发访问拦截，需要安全验证")
            deadline = time.monotonic() + max(15.0, float(timeout_s))
            next_hide = time.monotonic()
            while time.monotonic() < deadline:
                raise_if_cancelled(cancel_check)
                if latest is not None:
                    break
                href = str(page.url or "")
                if "console-ban" in href.lower():
                    raise_for_verification("C5GAME 触发访问拦截，需要安全验证")
                if time.monotonic() >= next_hide:
                    _hide_browser_windows(self._proc)
                    next_hide = time.monotonic() + 1.5
                page.wait_for_timeout(400)
            else:
                raise RuntimeError("等待 C5GAME 挂单接口超时")
            _hide_browser_windows(self._proc)
        finally:
            try:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass

        assert latest is not None
        if captured_headers:
            self._headers = captured_headers
            try:
                from core.market_candidates import save_c5_client_headers

                save_c5_client_headers(captured_headers)
            except Exception:
                pass
        risk = _risk_text(latest)
        if _looks_like_risk(risk):
            raise_for_verification(
                f"C5GAME 需要安全验证：{risk.strip() or '风控'}"
            )
        # After sniff, prefer the exact page_no via HTTP (page>1 must not return
        # the first-page XHR body from navigation).
        if self._headers:
            refreshed = fetch_with_headers(announce=False)
            if refreshed is not None:
                return refreshed
            if page_no > 1:
                raise RuntimeError(
                    f"C5GAME 嗅探后仍无法拉取第 {page_no} 页挂单"
                )
        return latest


def get_c5_browser_collector() -> C5BrowserCollector:
    collector = getattr(_TLS, "collector", None)
    if collector is not None and not collector._worker.is_alive():
        try:
            collector.close()
        except Exception:
            pass
        collector = None
        _TLS.collector = None
    if collector is None:
        collector = C5BrowserCollector()
        _TLS.collector = collector
    return collector


def close_c5_browser_collector() -> None:
    collector = getattr(_TLS, "collector", None)
    if collector is None:
        return
    try:
        collector.close()
    finally:
        _TLS.collector = None
