"""Win32：置顶（SetWindowPos）、沉浸式深色标题栏（DwmSetWindowAttribute）。"""

from __future__ import annotations


def _refresh_non_client_area_win32(hwnd: int) -> None:
    """强制重绘标题栏等非客户区，避免主题切换要等窗口重新激活才生效。"""
    if hwnd == 0:
        return
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return
    user32 = ctypes.windll.user32
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(0),
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )
    RDW_INVALIDATE = 0x0001
    RDW_UPDATENOW = 0x0100
    RDW_FRAME = 0x0400
    user32.RedrawWindow(
        wintypes.HWND(hwnd),
        None,
        None,
        RDW_INVALIDATE | RDW_UPDATENOW | RDW_FRAME,
    )
    WM_NCACTIVATE = 0x0086
    current_active = int(user32.GetActiveWindow()) == hwnd
    # 某些 Win10/11 构建里，仅 RedrawWindow 仍不会立刻刷新标题栏主题；
    # 额外模拟一次非客户区激活态切换，可在不真正抢焦点的情况下强制 caption 重绘。
    user32.SendMessageW(
        wintypes.HWND(hwnd),
        WM_NCACTIVATE,
        wintypes.WPARAM(0 if current_active else 1),
        wintypes.LPARAM(0),
    )
    user32.SendMessageW(
        wintypes.HWND(hwnd),
        WM_NCACTIVATE,
        wintypes.WPARAM(1 if current_active else 0),
        wintypes.LPARAM(0),
    )


def try_set_topmost_win32(window, on: bool) -> bool:
    """SetWindowPos TOPMOST/NOTOPMOST；成功返回 True 时不应再对同一窗口 setWindowFlags(WindowStaysOnTopHint)。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    try:
        hwnd = int(window.winId())
    except (TypeError, ValueError):
        return False
    if hwnd == 0:
        return False
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    insert = HWND_TOPMOST if on else HWND_NOTOPMOST
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    user32 = ctypes.windll.user32
    r = user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert),
        0,
        0,
        0,
        0,
        flags,
    )
    return bool(r)


def is_native_topmost_hwnd_win32(hwnd: int) -> bool:
    """当前 HWND 是否带 WS_EX_TOPMOST（与 SetWindowPos TOPMOST 一致）。"""
    if hwnd == 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    user32 = ctypes.windll.user32
    gl = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    ex = int(gl(wintypes.HWND(hwnd), GWL_EXSTYLE))
    return bool(ex & WS_EX_TOPMOST)


def is_window_native_topmost_win32(window) -> bool:
    """顶层 QWidget 对应原生窗口是否为 TOPMOST（置顶图钉走 Win32 时主窗无 Qt StaysOnTopHint，需用此判断）。"""
    try:
        hwnd = int(window.winId())
    except (TypeError, ValueError):
        return False
    return is_native_topmost_hwnd_win32(hwnd)


# DWMWA_USE_IMMERSIVE_DARK_MODE：新 SDK 为 20；部分 Win10 构建曾用 19
_DWM_IMMERSIVE_DARK_ATTRS: tuple[int, ...] = (20, 19)


def try_apply_immersive_dark_title_bar_win32(window, dark: bool) -> bool:
    """将原生标题栏切到沉浸式深色/浅色，与 Qt 内「深色/浅色」主题一致。

    依赖 ``dwmapi``；失败时静默返回 False（HWND 未就绪等）。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    try:
        hwnd = int(window.winId())
    except (TypeError, ValueError):
        return False
    if hwnd == 0:
        return False
    try:
        dwm = ctypes.windll.dwmapi
    except OSError:
        return False
    enable = ctypes.c_int(1 if dark else 0)
    ok = False
    for attr in _DWM_IMMERSIVE_DARK_ATTRS:
        hr = int(
            dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(enable),
                ctypes.sizeof(enable),
            )
        )
        if hr == 0:
            ok = True
            break
    if not ok:
        return False
    _refresh_non_client_area_win32(hwnd)
    return True
