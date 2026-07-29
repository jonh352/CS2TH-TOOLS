"""app_settings.json 中的 Playwright 首选浏览器（chrome / msedge）；供 core 启动浏览器时读取。"""

from __future__ import annotations

from config import APP_SETTINGS_PREFERRED_PLAYWRIGHT_CHANNEL_KEY

from .app_settings_store import load_app_settings, update_app_settings

PLAYWRIGHT_CHANNEL_CHROME = "chrome"
PLAYWRIGHT_CHANNEL_MSEDGE = "msedge"


def load_preferred_playwright_channel() -> str:
    """返回 ``chrome`` 或 ``msedge``；无记录或非法时默认 ``msedge``（与历史行为一致）。"""
    v = str(
        load_app_settings().get(APP_SETTINGS_PREFERRED_PLAYWRIGHT_CHANNEL_KEY) or ""
    ).strip().lower()
    if v == PLAYWRIGHT_CHANNEL_CHROME:
        return PLAYWRIGHT_CHANNEL_CHROME
    if v in (PLAYWRIGHT_CHANNEL_MSEDGE, "edge"):
        return PLAYWRIGHT_CHANNEL_MSEDGE
    return PLAYWRIGHT_CHANNEL_MSEDGE


def load_playwright_channel_try_order() -> tuple[str, str]:
    """先试首选渠道，失败再试另一种（均为 Playwright ``channel`` 名）。"""
    first = load_preferred_playwright_channel()
    second = (
        PLAYWRIGHT_CHANNEL_CHROME
        if first == PLAYWRIGHT_CHANNEL_MSEDGE
        else PLAYWRIGHT_CHANNEL_MSEDGE
    )
    return (first, second)


def save_preferred_playwright_channel(channel: str) -> None:
    if channel not in (PLAYWRIGHT_CHANNEL_CHROME, PLAYWRIGHT_CHANNEL_MSEDGE):
        return
    update_app_settings(
        updates={APP_SETTINGS_PREFERRED_PLAYWRIGHT_CHANNEL_KEY: channel}
    )
