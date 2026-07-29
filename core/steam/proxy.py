"""系统 HTTP 代理解析（urllib / 环境变量）。"""

from urllib.request import getproxies


def resolve_system_http_proxy_for_steam() -> str:
    """
    每次调用重新读取，便于 VPN 切换端口后即时生效。
    无配置时返回空串（直连）。
    """
    proxies = getproxies()
    raw = (proxies.get("https") or proxies.get("http") or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://", "socks5://", "socks4://")):
        raw = f"http://{raw}"
    return raw
