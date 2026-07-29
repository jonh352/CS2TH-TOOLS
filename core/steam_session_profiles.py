"""Steam 多账号 / 单账号目录：元数据路径与轻量登录态文件位置。

``session_dir``（账号根目录）由调用方传入时，轻量状态文件布局与
``profile_browser_dir(...)`` 下一致，便于后续多账号 UI 只改传入的根路径。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

STEAM_STORAGE_STATE_FILENAME = "steam_storage_state.json"
LOGIN_SESSION_SUBDIR = "_login_session"


class ProfileRealm(str, Enum):
    """与库存、数据采集等场景对应的 profile 分区（预留多账号扩展）。"""

    INVENTORY = "inventory"
    FETCH = "fetch"


def profile_browser_dir(realm: ProfileRealm, profile_id: str) -> Path:
    """单账号 Steam 相关文件的根目录（与旧版「每账号一个浏览器目录」路径语义对齐，可共存元数据）。"""
    from config import BROWSER_DIR, INVENTORY_DIR

    pid = (profile_id or "").strip() or "default"
    if realm == ProfileRealm.INVENTORY:
        root = INVENTORY_DIR / "steam_profiles" / realm.value
    else:
        root = BROWSER_DIR / "steam_profiles" / realm.value
    return (root / pid).resolve()


def profile_storage_state_path(realm: ProfileRealm, profile_id: str) -> Path:
    return profile_browser_dir(realm, profile_id) / STEAM_STORAGE_STATE_FILENAME


def profile_temp_login_session_dir(realm: ProfileRealm, profile_id: str) -> Path:
    """交互登录时使用的临时 Playwright user_data_dir；导出 storage_state 后应删除。"""
    return profile_browser_dir(realm, profile_id) / LOGIN_SESSION_SUBDIR


def steam_account_storage_state_path(account_root: Path | str) -> Path:
    """任意账号根目录下的轻量登录态路径（与 profile_storage_state_path 布局一致）。"""
    return Path(account_root).resolve() / STEAM_STORAGE_STATE_FILENAME


def steam_account_temp_login_session_dir(account_root: Path | str) -> Path:
    return Path(account_root).resolve() / LOGIN_SESSION_SUBDIR
