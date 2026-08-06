"""Central configuration for CS2TH 汰换小助手.

Runtime data is kept outside the installation directory so packaged builds remain
read-only and upgrades do not discard user sessions.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

APP_NAME = "CS2TH 汰换小助手"
APP_VERSION = "0.3.3"
# HTTP/1.1 headers are encoded as Latin-1 by requests/urllib3. Keep the
# transport identifier ASCII-only even though the visible application name is Chinese.
APP_HTTP_USER_AGENT = f"CS2TH-Tradeup-Assistant/{APP_VERSION}"


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller onedir/onefile: bundled assets live under _MEIPASS.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = _bundle_root()
ASSETS_DIR = PROJECT_ROOT / "assets"
META_DIR = PROJECT_ROOT / "meta"
WEAPON_IMAGES_DIR = PROJECT_ROOT / "weapon_images"
APP_ICON = ASSETS_DIR / "logo.ico"
BRAND_IMAGE = ASSETS_DIR / "brand-th.png"

_appdata = Path(os.environ.get("APPDATA") or Path.home())
CACHE_DIR = _appdata / "CS2TH" / "Tools"
PREFS_DIR = CACHE_DIR / "prefs"
INVENTORY_DIR = CACHE_DIR / "inventory"
BROWSER_DIR = CACHE_DIR / "browser"
ALCHEMY_CACHE_DIR = CACHE_DIR / "alchemy"
RECIPES_DIR = CACHE_DIR / "recipes"
COLLECTED_JSON_DIR = CACHE_DIR / "collected_json"
STEAM_SESSION_DIR = BROWSER_DIR / "steam"
STEAM_AVATAR_CACHE_DIR = BROWSER_DIR / "steam_avatars"

APP_SETTINGS_FILE = PREFS_DIR / "app_settings.json"
AUTH_SESSION_FILE = PREFS_DIR / "auth_session.json"
INVENTORY_FILE = INVENTORY_DIR / "inventory.json"
INVENTORY_CONFIG_FILE = INVENTORY_DIR / "inventory_config.json"
STEAM_ACCOUNTS_INDEX_FILE = INVENTORY_DIR / "steam_accounts_index.json"
INVENTORY_HIDE_NAMES_FILE = META_DIR / "inventory_hide_names.json"
APP_SETTINGS_PREFERRED_PLAYWRIGHT_CHANNEL_KEY = "preferred_playwright_channel"
APP_SETTINGS_CLOSE_BEHAVIOR_KEY = "close_behavior"
CLOSE_BEHAVIOR_MINIMIZE = "minimize"
CLOSE_BEHAVIOR_EXIT = "exit"
PRODUCT_PRICE_FILE = ALCHEMY_CACHE_DIR / "product_price_all.json"
PRODUCT_PRICE_HTTP_META_FILE = ALCHEMY_CACHE_DIR / "product_price_http_meta.json"
MATERIAL_COLLECTION_HISTORY_FILE = CACHE_DIR / "material_collection_history.json"
_local_price_snapshot_env = os.environ.get(
    "CS2TH_TOOLS_LOCAL_PRICE_SNAPSHOT",
    "",
).strip()
LOCAL_PRODUCT_PRICE_SNAPSHOT: Path | None = (
    Path(_local_price_snapshot_env) if _local_price_snapshot_env else None
)

# Account login and desktop price delivery are live on cs2th.cn.
AUTH_API_ENABLED = os.environ.get("CS2TH_TOOLS_AUTH_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
}
AUTH_API_BASE_URL = os.environ.get("CS2TH_TOOLS_API_BASE", "https://cs2th.cn").rstrip("/")
PRICE_API_ENABLED = os.environ.get("CS2TH_TOOLS_PRICE_ENABLED", "1").lower() in {
    "1",
    "true",
    "yes",
}
AUTH_HTTP_TIMEOUT_S = 12.0
PRODUCT_PRICE_API = f"{AUTH_API_BASE_URL}/api/desktop/product-price"

# Feature-page assets and layout. The remaining Helper pages use these shared
# constants; keeping them here prevents per-page magic numbers.
ALCHEMY_ICON_PATH = ASSETS_DIR / "alchemy.svg"
RECIPE_ICON_PATH = ASSETS_DIR / "recipe.svg"
EXPORT_ICON_PATH = ASSETS_DIR / "export.svg"
SLIDER_HANDLE_SVG_PATH = ASSETS_DIR / "slider.svg"
ALCHEMY_SIMULATION_ICON_PATH = ASSETS_DIR / "simulation.svg"
FETCH_PLATFORM_LOGO_PATHS = {
    "buff": ASSETS_DIR / "buff_logo.png",
    "yyyp": ASSETS_DIR / "yyyp_logo.png",
    "c5": ASSETS_DIR / "c5game_logo.png",
    "eco": ASSETS_DIR / "eco_logo.png",
    "steam": ASSETS_DIR / "steam_logo.jpg",
}

TITLE_BAR_HEIGHT = 32
CONTENT_PAGE_LAYOUT_MARGINS = (26, 22, 26, 10)
SPECIAL_WEAR_SEARCH_WIDTH = 320
ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS = 1
ALCHEMY_SCAN_MODE_DINKELBACH_TOPK = 20
ALCHEMY_TARGET_MODE_DINKELBACH_TOPK = 50
ALCHEMY_RESULT_DISPLAY_TOP_N = 10
# Bound process fan-out on high-core Windows hosts to protect UI responsiveness.
ALCHEMY_SCAN_MODE_PROCESS_POOL_MAX_WORKERS = 8
ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH = 196
ALCHEMY_SIMULATION_SKIN_SEARCH_MIN_WIDTH = 168
TOAST_TOP_MARGIN = 12
TOAST_DURATION_MS = 2200
TOAST_ANIM_DURATION = 180

SOFTWARE_LOGIN_PRODUCT_PRICE_ERROR_MESSAGE = (
    "暂时无法获取 CS2TH 价格，请登录有使用权限的 CS2TH 账号后重试"
)

STEAM_BROWSER_LOGIN_PAGE_TIMEOUT_S = 60
STEAM_BROWSER_LOGIN_COMPLETE_WAIT_S = 300
STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S = 45
STEAM_BROWSER_PROFILE_BY_STEAM_ID_TIMEOUT_S = 45
STEAM_BROWSER_MY_PROFILE_TIMEOUT_S = 30
STEAM_BROWSER_PROFILE_SELECTOR_TIMEOUT_S = 12

PLAYWRIGHT_EDGE_USER_DATA_DIR: Path | None = None
PLAYWRIGHT_CHROME_USER_DATA_DIR: Path | None = None
PLAYWRIGHT_MSEDGE_USE_SYSTEM_USER_DATA = False
PLAYWRIGHT_CHROME_USE_SYSTEM_USER_DATA = False
PLAYWRIGHT_USER_AGENT = ""

PLAYWRIGHT_STEALTH_PATCH_DEVTOOLS = False
PLAYWRIGHT_STEALTH_MUTE_CONSOLE = True
PLAYWRIGHT_STEALTH_MODIFY_WEB_UK = True
PLAYWRIGHT_STEALTH_CANVAS_NOISE = True
PLAYWRIGHT_STEALTH_WEBGL_NOISE = True
PLAYWRIGHT_WEBGL_VENDOR: str | None = None
PLAYWRIGHT_WEBGL_RENDERER: str | None = None
PLAYWRIGHT_STEALTH_PATCH_NAVIGATOR_PRODUCT = True
PLAYWRIGHT_NAVIGATOR_VENDOR: str | None = "Google Inc."
PLAYWRIGHT_NAVIGATOR_VENDOR_SUB: str | None = ""
PLAYWRIGHT_NAVIGATOR_PRODUCT: str | None = "Gecko"
PLAYWRIGHT_NAVIGATOR_PRODUCT_SUB: str | None = "20030107"
PLAYWRIGHT_NAVIGATOR_APP_CODE_NAME: str | None = "Mozilla"
PLAYWRIGHT_NAVIGATOR_APP_NAME: str | None = "Netscape"
PLAYWRIGHT_NAVIGATOR_APP_NAME_RANDOM = False
PLAYWRIGHT_NAVIGATOR_BRANDS: list[dict[str, str]] | None = None
PLAYWRIGHT_NAVIGATOR_APP_VERSION: str | None = None
PLAYWRIGHT_NAVIGATOR_PLATFORM: str | None = None
PLAYWRIGHT_NAVIGATOR_HARDWARE_CONCURRENCY: int | None = None
PLAYWRIGHT_NAVIGATOR_DEVICE_MEMORY: int | float | None = None
PLAYWRIGHT_NAVIGATOR_PLUGINS_LENGTH: int | None = None
PLAYWRIGHT_NAVIGATOR_LANGUAGES: list[str] | None = ["zh-CN", "en", "en-GB", "en-US"]


def _physical_screen_size() -> tuple[int, int]:
    try:
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return 1920, 1080


PLAYWRIGHT_PHYSICAL_SCREEN_WIDTH, PLAYWRIGHT_PHYSICAL_SCREEN_HEIGHT = _physical_screen_size()
PLAYWRIGHT_SCREEN_WIDTH = PLAYWRIGHT_PHYSICAL_SCREEN_WIDTH
PLAYWRIGHT_SCREEN_HEIGHT = PLAYWRIGHT_PHYSICAL_SCREEN_HEIGHT
PLAYWRIGHT_RANDOM_SIZE = True
PLAYWRIGHT_SCREEN_AVAIL_WIDTH: int | None = None
PLAYWRIGHT_SCREEN_AVAIL_HEIGHT: int | None = None
PLAYWRIGHT_WINDOW_INNER_WIDTH: int | None = None
PLAYWRIGHT_WINDOW_INNER_HEIGHT: int | None = None
PLAYWRIGHT_WINDOW_OUTER_WIDTH: int | None = None
PLAYWRIGHT_WINDOW_OUTER_HEIGHT: int | None = None
PLAYWRIGHT_WINDOW_DEVICE_PIXEL_RATIO: int | float | None = None


def ensure_runtime_dirs() -> None:
    for path in (
        CACHE_DIR,
        PREFS_DIR,
        INVENTORY_DIR,
        BROWSER_DIR,
        STEAM_AVATAR_CACHE_DIR,
        ALCHEMY_CACHE_DIR,
        RECIPES_DIR,
        COLLECTED_JSON_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
