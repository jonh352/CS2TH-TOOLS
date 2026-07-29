"""运行时窗口/屏幕尺寸解析（可按配置随机）。"""

from __future__ import annotations

import random

from config import (
    PLAYWRIGHT_PHYSICAL_SCREEN_HEIGHT,
    PLAYWRIGHT_PHYSICAL_SCREEN_WIDTH,
    PLAYWRIGHT_RANDOM_SIZE,
    PLAYWRIGHT_SCREEN_AVAIL_HEIGHT,
    PLAYWRIGHT_SCREEN_AVAIL_WIDTH,
    PLAYWRIGHT_SCREEN_HEIGHT,
    PLAYWRIGHT_SCREEN_WIDTH,
    PLAYWRIGHT_WINDOW_DEVICE_PIXEL_RATIO,
    PLAYWRIGHT_WINDOW_INNER_HEIGHT,
    PLAYWRIGHT_WINDOW_INNER_WIDTH,
    PLAYWRIGHT_WINDOW_OUTER_HEIGHT,
    PLAYWRIGHT_WINDOW_OUTER_WIDTH,
)

_RUNTIME_METRICS: dict[str, int | float | None] | None = None


def get_runtime_window_metrics() -> dict[str, int | float | None]:
    """返回本次进程内固定的窗口/屏幕参数。"""
    global _RUNTIME_METRICS
    if _RUNTIME_METRICS is not None:
        return _RUNTIME_METRICS

    screen_width = PLAYWRIGHT_SCREEN_WIDTH
    screen_height = PLAYWRIGHT_SCREEN_HEIGHT
    inner_width = PLAYWRIGHT_WINDOW_INNER_WIDTH
    inner_height = PLAYWRIGHT_WINDOW_INNER_HEIGHT

    if PLAYWRIGHT_RANDOM_SIZE:
        # screen大小，肉眼能看出变化
        sw_min = max(1, int(PLAYWRIGHT_SCREEN_WIDTH // 2) - 5)
        sw_max = min(PLAYWRIGHT_PHYSICAL_SCREEN_WIDTH, int(PLAYWRIGHT_SCREEN_WIDTH // 2) + 5)
        sh_min = max(1, int(PLAYWRIGHT_SCREEN_HEIGHT // 2))
        sh_max = min(PLAYWRIGHT_PHYSICAL_SCREEN_HEIGHT, int(PLAYWRIGHT_SCREEN_HEIGHT // 2) + 5)
        screen_width = random.randint(sw_min, sw_max)
        screen_height = random.randint(sh_min, sh_max)

        # window大小，肉眼看不出变化
        iw_min = max(1, int(screen_width * 0.1))
        ih_min = max(1, int(screen_height * 0.2))
        inner_width = random.randint(iw_min, int(screen_width))
        inner_height = random.randint(ih_min, int(screen_height))

    _RUNTIME_METRICS = {
        "screen_width": screen_width,
        "screen_height": screen_height,
        "screen_avail_width": PLAYWRIGHT_SCREEN_AVAIL_WIDTH,
        "screen_avail_height": PLAYWRIGHT_SCREEN_AVAIL_HEIGHT,
        "window_inner_width": inner_width,
        "window_inner_height": inner_height,
        "window_outer_width": PLAYWRIGHT_WINDOW_OUTER_WIDTH,
        "window_outer_height": PLAYWRIGHT_WINDOW_OUTER_HEIGHT,
        "window_device_pixel_ratio": PLAYWRIGHT_WINDOW_DEVICE_PIXEL_RATIO,
    }
    return _RUNTIME_METRICS

