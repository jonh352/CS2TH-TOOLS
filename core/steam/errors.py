"""Steam 浏览器与会话相关异常。"""

STEAM_FETCH_SESSION_EXPIRED = "__STEAM_FETCH_SESSION_EXPIRED__"
STEAM_BROWSER_NOT_INSTALLED_MSG = "请先安装Edge或Chrome浏览器"


class SteamSessionExpiredError(RuntimeError):
    """库存 API 返回会话无效（未登录或 Cookie 失效）。"""


class SteamBrowserNotFoundError(RuntimeError):
    """本机无法通过 Playwright 启动 Edge 或 Chrome。"""


class SteamInventoryFetchCancelledError(RuntimeError):
    """库存拉取被用户取消或超时放弃。"""
