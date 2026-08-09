"""Launch a real system Chrome/Edge for marketplace login (no Playwright CDP).

C5GAME's /console-ban page treats Playwright-controlled windows as「开发者模式」.
Login therefore uses a normal browser process; cookies are harvested after it exits.
"""

from __future__ import annotations

import os
import mmap
import re
import shutil
import sqlite3
import subprocess
import time
import ctypes
from pathlib import Path
from typing import Any, Callable

from core.playwright_channel_prefs import load_playwright_channel_try_order
from core.steam.launch import (
    _clear_stale_profile_singletons,
    clear_chromium_session_restore,
    focus_single_page,
    launch_persistent_chromium_context,
)

ProgressCb = Callable[[str], None] | None


def _windows_browser_candidates(channel: str) -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    if channel == "chrome":
        return [
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    return [
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]


def resolve_system_browser_executable() -> Path | None:
    """Prefer the app's Playwright channel order, then PATH lookup."""
    for channel in load_playwright_channel_try_order():
        for path in _windows_browser_candidates(channel):
            if path.is_file():
                return path
        which = shutil.which("chrome" if channel == "chrome" else "msedge")
        if which:
            return Path(which)
    for name in ("chrome", "msedge", "google-chrome", "chromium"):
        which = shutil.which(name)
        if which:
            return Path(which)
    return None


def launch_system_browser(
    *, profile_dir: Path, url: str, net_log_path: Path | None = None
) -> subprocess.Popen:
    exe = resolve_system_browser_executable()
    if exe is None:
        raise RuntimeError(
            "未找到本机 Chrome / Edge。请安装浏览器后重试，或在设置里切换首选浏览器"
        )
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    clear_chromium_session_restore(profile_dir)
    _clear_stale_profile_singletons(profile_dir)
    # No remote-debugging / automation flags — a normal user window.
    args = [
        str(exe),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "--window-size=1280,840",
    ]
    if net_log_path is not None:
        args.extend(
            [
                f"--log-net-log={Path(net_log_path)}",
                "--net-log-capture-mode=IncludeSensitive",
            ]
        )
    args.append(str(url))
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def harvest_c5_netlog_headers(path: Path) -> dict[str, str]:
    """Extract only reusable C5 client markers from a closed Chromium NetLog."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return {}
    wanted = {
        "x-app-channel",
        "x-device-id",
        "x-source",
        "x-area",
        "x-traffic-tag",
        "x-device-os",
        "x-device-model",
        "user-agent",
    }
    found: dict[str, str] = {}
    pattern = re.compile(rb'"([A-Za-z0-9-]+): ([^"\\]*)"')
    try:
        with path.open("rb") as stream, mmap.mmap(
            stream.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            for match in pattern.finditer(data):
                key = match.group(1).decode("ascii", errors="ignore").lower()
                if key not in wanted:
                    continue
                value = match.group(2).decode("utf-8", errors="ignore").strip()
                if value:
                    found["User-Agent" if key == "user-agent" else key] = value
    except (OSError, ValueError):
        return {}
    return found


def c5_netlog_login_ready(path: Path) -> bool:
    """Detect a token-bearing C5 API request that received a successful response."""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as stream, mmap.mmap(
            stream.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            needle = b'x-access-token: '
            end_at = len(data)
            while end_at > 0:
                position = data.rfind(needle, 0, end_at)
                if position < 0:
                    return False
                start = position + len(needle)
                end = data.find(b'"', start)
                if end < 0:
                    end_at = position
                    continue
                value = bytes(data[start:end]).strip()
                if len(value) >= 20:
                    source_at = data.find(b'"source":{"id":', end, end + 4096)
                    if source_at >= 0:
                        id_start = source_at + len(b'"source":{"id":')
                        id_end = data.find(b',', id_start, id_start + 32)
                        source_id = bytes(data[id_start:id_end]).strip()
                        if source_id.isdigit():
                            source_marker = b'"source":{"id":' + source_id
                            response_at = data.find(source_marker, source_at + 1)
                            while response_at >= 0:
                                response = data[max(0, response_at - 2200):response_at]
                                if (
                                    b'HTTP/1.1 200' in response
                                    or b':status: 200' in response
                                ) and b'application/json' in response:
                                    return True
                                response_at = data.find(
                                    source_marker, response_at + len(source_marker)
                                )
                end_at = position
    except (OSError, ValueError):
        return False
    return False


def c5_profile_login_marker(profile_dir: Path) -> tuple[tuple[str, int, int, str], ...]:
    """Return a non-secret marker for C5 auth cookies in a live profile.

    Chromium may buffer NetLog writes until shutdown. Cookie names and encrypted
    value lengths are readable from its SQLite database without decrypting or
    copying the credential. The update stamp lets login capture distinguish a
    newly written token from a stale cookie that was already present at launch.
    """
    profile = Path(profile_dir)
    candidates = (
        profile / "Default" / "Network" / "Cookies",
        profile / "Default" / "Cookies",
    )
    auth_names = {
        "nc5_accesstoken",
        "c5token",
        "access_token",
        "ncaccess",
        "token",
        "authorization",
    }
    for path in candidates:
        if not path.is_file():
            continue
        connection = None
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro",
                uri=True,
                timeout=0.5,
            )
            columns = {
                str(row[1]).lower()
                for row in connection.execute("PRAGMA table_info(cookies)")
            }
            stamp_column = (
                "last_update_utc"
                if "last_update_utc" in columns
                else "expires_utc"
                if "expires_utc" in columns
                else "''"
            )
            rows = connection.execute(
                "SELECT name, length(value), length(encrypted_value), "
                f"{stamp_column} "
                "FROM cookies "
                "WHERE (lower(host_key) LIKE '%c5game.com%' "
                "OR lower(host_key) LIKE '%zbt.com%')"
            )
            markers: list[tuple[str, int, int, str]] = []
            for name, value_size, encrypted_size, stamp in rows:
                if (
                    str(name or "").strip().lower() in auth_names
                    and (int(value_size or 0) > 0 or int(encrypted_size or 0) > 0)
                ):
                    markers.append(
                        (
                            str(name or "").strip().lower(),
                            int(value_size or 0),
                            int(encrypted_size or 0),
                            str(stamp or ""),
                        )
                    )
            if markers:
                return tuple(sorted(markers))
        except (OSError, sqlite3.Error, ValueError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return ()


def c5_profile_login_ready(profile_dir: Path) -> bool:
    """Return whether the profile currently contains a C5 auth cookie."""
    return bool(c5_profile_login_marker(profile_dir))


def _close_browser_window(proc: subprocess.Popen) -> bool:
    """Ask the top-level Chromium window to close so its profile is flushed."""
    if os.name != "nt":
        return False
    posted = False
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        nonlocal posted
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == proc.pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            posted = True
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        return False
    return posted


def wait_browser_closed(
    proc: subprocess.Popen,
    *,
    timeout_s: float = 420.0,
    poll_s: float = 0.8,
    progress: ProgressCb = None,
    progress_message: str = "",
    auto_close_when: Callable[[], bool] | None = None,
    auto_close_message: str = "",
) -> bool:
    """Return True if the browser process exited before timeout."""
    deadline = time.monotonic() + max(30.0, timeout_s)
    last_emit = 0.0
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            return True
        now = time.monotonic()
        if auto_close_when is not None:
            try:
                ready = bool(auto_close_when())
            except Exception:
                ready = False
            if ready:
                if progress and auto_close_message:
                    progress(auto_close_message)
                _close_browser_window(proc)
                close_deadline = time.monotonic() + 12.0
                while time.monotonic() < close_deadline:
                    if proc.poll() is not None:
                        return True
                    time.sleep(0.2)
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
                return proc.poll() is not None
        if progress and progress_message and now - last_emit >= 8.0:
            progress(progress_message)
            last_emit = now
        time.sleep(poll_s)
    try:
        proc.terminate()
    except Exception:
        pass
    return False


def harvest_profile_cookies(
    profile_dir: Path,
    *,
    domain_hints: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Read cookies from a closed Chromium profile without opening any website."""
    from playwright.sync_api import sync_playwright

    from core.steam.errors import SteamBrowserLaunchError, SteamBrowserNotFoundError

    cookies: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            context = launch_persistent_chromium_context(
                playwright,
                Path(profile_dir),
                headless=True,
                viewport=None,
                webdriver_stealth=False,
                market_browser=True,
                isolated_profile=True,
            )
            try:
                focus_single_page(context)
                raw = list(context.cookies() or [])
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except (SteamBrowserNotFoundError, SteamBrowserLaunchError) as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取登录 Cookie 失败：{exc}") from exc

    hints = [h.lower() for h in domain_hints if h]
    for item in raw:
        if not isinstance(item, dict):
            continue
        if hints:
            domain = str(item.get("domain") or "").lower()
            if not any(h in domain for h in hints):
                continue
        cookies.append(item)
    return cookies
