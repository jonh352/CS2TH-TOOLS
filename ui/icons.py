"""SVG 图标加载 - 运行时替换 currentColor"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

from config import ASSETS_DIR

from ui.asset_cache import cached_asset_qicon_svg

_TRIANGLE_FILL_PATH = ASSETS_DIR / "triangle-fill.svg"
# (rotation_deg, size_px, fill) -> QIcon；展开/收起指示器复用 triangle-fill.svg 旋转绘制
_triangle_rotated_cache: dict[tuple[float, int, str], QIcon] = {}


def _svg_mtime_ns(svg_path: Path) -> int:
    """用于缓存键：替换 assets 下同名文件后 mtime 变化，避免 lru_cache 仍返回旧栅格。"""
    try:
        st = svg_path.stat()
        ns = getattr(st, "st_mtime_ns", None)
        if ns is not None:
            return int(ns)
        return int(st.st_mtime * 1_000_000_000)
    except OSError:
        return 0


@lru_cache(maxsize=512)
def _load_svg_icon_tinted_cached(
    path_resolved: str, color: str, size: int, mtime_ns: int
) -> QIcon:
    """按路径+修改时间+着色+尺寸缓存栅格化结果；大量重复加载时仍避免反复读盘。"""
    svg_path = Path(path_resolved)
    if not svg_path.is_file():
        return QIcon()
    try:
        content = svg_path.read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    content = content.replace('fill="currentColor"', f'fill="{color}"')
    content = content.replace("fill='currentColor'", f"fill='{color}'")
    content = content.replace('stroke="currentColor"', f'stroke="{color}"')
    content = content.replace("stroke='currentColor'", f"stroke='{color}'")
    data = content.encode("utf-8")
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        return cached_asset_qicon_svg(svg_path)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, pixmap.rect())
    painter.end()
    return QIcon(pixmap)


def load_svg_icon(svg_path: Path, color: str, size: int = 12) -> QIcon:
    """加载 SVG 并替换 currentColor 为指定颜色"""
    if not svg_path or not svg_path.exists():
        return QIcon()
    try:
        key_path = str(svg_path.resolve())
    except OSError:
        key_path = str(svg_path)
    svg_path_key = Path(key_path)
    mtime_ns = _svg_mtime_ns(svg_path_key)
    return _load_svg_icon_tinted_cached(key_path, color, int(size), mtime_ns)


def triangle_fill_icon_rotated(
    rotation_degrees: float,
    *,
    size_px: int = 14,
    fill_color: str = "#000000",
) -> QIcon:
    """``assets/triangle-fill.svg`` 尖端默认朝上，绕中心旋转 ``rotation_degrees`` 后栅格化为方形图标。

    ``fill_color`` 替换 SVG 主 path 的 ``fill=\"#000000\"``，便于随主题/强调色着色。
    """
    key = (round(rotation_degrees, 2), int(size_px), fill_color)
    cached = _triangle_rotated_cache.get(key)
    if cached is not None:
        return cached

    if not _TRIANGLE_FILL_PATH.is_file():
        empty = QIcon()
        _triangle_rotated_cache[key] = empty
        return empty

    try:
        content = _TRIANGLE_FILL_PATH.read_text(encoding="utf-8")
    except OSError:
        empty = QIcon()
        _triangle_rotated_cache[key] = empty
        return empty

    content = content.replace('fill="#000000"', f'fill="{fill_color}"')
    data = content.encode("utf-8")
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        empty = QIcon()
        _triangle_rotated_cache[key] = empty
        return empty

    sz = int(size_px)
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(sz / 2, sz / 2)
    p.rotate(rotation_degrees)
    p.translate(-sz / 2, -sz / 2)
    renderer.render(p, QRectF(0, 0, sz, sz))
    p.end()
    icon = QIcon(pm)
    _triangle_rotated_cache[key] = icon
    return icon


def expand_section_triangle_icon(
    expanded: bool,
    *,
    size_px: int = 14,
    fill_color: str = "#000000",
) -> QIcon:
    """垂直方向展开区块：收起 = 尖端朝右（90°）、展开 = 朝下（180°），与排除配方弹窗文件夹行一致。"""
    deg = 180.0 if expanded else 90.0
    return triangle_fill_icon_rotated(deg, size_px=size_px, fill_color=fill_color)


def sidebar_toggle_triangle_icon(
    sidebar_expanded: bool,
    *,
    size_px: int = 14,
    fill_color: str = "#000000",
) -> QIcon:
    """侧栏展开/收起：窄栏时尖端朝右（90°）；整栏时尖端朝左（270°）。"""
    deg = 270.0 if sidebar_expanded else 90.0
    return triangle_fill_icon_rotated(deg, size_px=size_px, fill_color=fill_color)
