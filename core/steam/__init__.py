"""
Steam 会话（Playwright）、库存 API 与解析。

应用层请自本包导入，勿依赖内部模块路径。
"""

from .browser_session import (
    clear_steam_session,
    get_steam_cookies,
    get_steam_cookies_from_storage_state,
    login_steam_session,
    login_steam_session_to_storage_state,
)
from .constants import DEFAULT_SESSION_DIR
from .errors import (
    STEAM_BROWSER_NOT_INSTALLED_MSG,
    STEAM_BROWSER_PROFILE_BUSY_MSG,
    STEAM_FETCH_SESSION_EXPIRED,
    SteamBrowserLaunchError,
    SteamBrowserNotFoundError,
    SteamInventoryFetchCancelledError,
    SteamSessionExpiredError,
)
from .inventory_pipeline import (
    fetch_inventory,
    format_inventory_status_line,
    inventory_item_visible_on_page,
)
from .launch import (
    launch_persistent_chromium_context,
    persistent_launch_options,
    resolve_playwright_user_data_dir,
)
from .network import is_network_error
from .proxy import resolve_system_http_proxy_for_steam
from .stealth import playwright_stealth_init_js

__all__ = [
    "DEFAULT_SESSION_DIR",
    "STEAM_BROWSER_NOT_INSTALLED_MSG",
    "STEAM_BROWSER_PROFILE_BUSY_MSG",
    "STEAM_FETCH_SESSION_EXPIRED",
    "SteamBrowserLaunchError",
    "SteamBrowserNotFoundError",
    "SteamInventoryFetchCancelledError",
    "SteamSessionExpiredError",
    "clear_steam_session",
    "fetch_inventory",
    "format_inventory_status_line",
    "get_steam_cookies",
    "get_steam_cookies_from_storage_state",
    "inventory_item_visible_on_page",
    "is_network_error",
    "launch_persistent_chromium_context",
    "login_steam_session",
    "login_steam_session_to_storage_state",
    "persistent_launch_options",
    "playwright_stealth_init_js",
    "resolve_playwright_user_data_dir",
    "resolve_system_http_proxy_for_steam",
]
