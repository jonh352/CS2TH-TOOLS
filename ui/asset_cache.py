"""缓存 ``assets/`` 下静态位图与共享 ``QSvgRenderer``，减少重复读盘。

用户数据目录下的路径（如自定义头像）不进入全局缓存，避免替换文件后仍显示旧图。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer

from config import ASSETS_DIR

try:
    _ASSETS_RESOLVED = ASSETS_DIR.resolve()
except OSError:
    _ASSETS_RESOLVED = ASSETS_DIR


def is_asset_path(path: Path | str) -> bool:
    """路径（解析后）是否位于项目 ``assets`` 目录下。"""
    try:
        return Path(path).resolve().is_relative_to(_ASSETS_RESOLVED)
    except (OSError, ValueError):
        return False


def _resolved_str(path: Path | str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


# 位图：路径 -> 首包加载的 QPixmap（Qt 隐式共享，调用方勿原地改图）
_RASTER_PIXMAP_CACHE: dict[str, QPixmap] = {}


def cached_asset_pixmap(path: Path | str) -> QPixmap:
    """加载 ``assets`` 内位图并缓存；非 assets 路径每次从磁盘读取。"""
    p = Path(path)
    key = _resolved_str(p)
    if not is_asset_path(p):
        pm = QPixmap(key)
        return pm
    hit = _RASTER_PIXMAP_CACHE.get(key)
    if hit is not None and not hit.isNull():
        return hit
    pm = QPixmap(key)
    if not pm.isNull():
        _RASTER_PIXMAP_CACHE[key] = pm
    return pm


def cached_asset_qicon_raster(path: Path | str) -> QIcon:
    """由 assets 内 PNG 等构建 ``QIcon``（复用位图缓存）。"""
    pm = cached_asset_pixmap(path)
    if pm.isNull():
        return QIcon()
    return QIcon(pm)


@lru_cache(maxsize=48)
def _cached_qicon_svg_file(resolved_key: str) -> QIcon:
    return QIcon(resolved_key)


def cached_asset_qicon_svg(path: Path | str) -> QIcon:
    """未改色的 SVG 作为 ``QIcon`` 加载；仅对 ``assets`` 内路径做进程级缓存。"""
    p = Path(path)
    if not is_asset_path(p):
        return QIcon(_resolved_str(p))
    return _cached_qicon_svg_file(_resolved_str(p))


# 任意 assets 内 SVG 文件共享一个 QSvgRenderer（多实例控件共用，避免各读一遍盘）
_SVG_RENDERER_BY_KEY: dict[str, QSvgRenderer] = {}


def cached_shared_qsvg_renderer(path: Path | str) -> QSvgRenderer | None:
    """返回指向 ``path`` 的共享 ``QSvgRenderer``；文件缺失或无效时返回 ``None``。"""
    p = Path(path)
    if not p.is_file():
        return None
    if not is_asset_path(p):
        r = QSvgRenderer(_resolved_str(p))
        return r if r.isValid() else None
    key = _resolved_str(p)
    existing = _SVG_RENDERER_BY_KEY.get(key)
    if existing is not None and existing.isValid():
        return existing
    r = QSvgRenderer(key)
    if not r.isValid():
        return None
    _SVG_RENDERER_BY_KEY[key] = r
    return r
