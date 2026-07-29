"""与库存卡枪图区一致的武器图展示：渐变底、居中图标、品质色底条。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# 与 ``ui/pages/inventory.py`` 中品质色表保持一致
_RARITY_LINE_AND_TINT: dict[str, tuple[str, str]] = {
    "ancient": ("#eb4b4b", "#eb4b4b"),
    "legendary": ("#d32ce6", "#d32ce6"),
    "mythical": ("#8847ff", "#8847ff"),
    "rare": ("#4b69ff", "#4b69ff"),
    "uncommon": ("#5e98d9", "#5e98d9"),
    "common": ("#b0c3d9", "#b0c3d9"),
    "contraband": ("#ca8a04", "#fde047"),
}
_DEFAULT_ACCENT = ("#9ca3af", "#e5e7eb")

_QUALITY_CN_TO_RARITY: dict[str, str] = {
    "隐秘": "ancient",
    "保密": "legendary",
    "受限": "mythical",
    "军规级": "rare",
    "工业级": "uncommon",
    "消费级": "common",
    "非凡": "contraband",
}

_TINT_RATIO = 0.3
_MID_STOP_POS = 0.3
_IMAGE_MARGINS = (8, 8, 8, 0)
_BOTTOM_LINE_H = 3
_GRADIENT_TOP_WHITE = QColor("#ffffff")


def line_and_tint_for_quality_cn(quality_cn: str) -> tuple[str, str]:
    r = _QUALITY_CN_TO_RARITY.get((quality_cn or "").strip(), "")
    return _RARITY_LINE_AND_TINT.get(r, _DEFAULT_ACCENT)


def _grad_bottom_from_top(top: QColor, tint: QColor, ratio: float) -> str:
    t = max(0.0, min(1.0, ratio))
    r = int(round(top.red() * (1.0 - t) + tint.red() * t))
    g = int(round(top.green() * (1.0 - t) + tint.green() * t))
    b = int(round(top.blue() * (1.0 - t) + tint.blue() * t))
    return QColor(r, g, b).name()


def _palette_role_color(widget: QWidget, role: QPalette.ColorRole) -> QColor:
    """与主题一致：优先应用级 palette（子控件上直接取色在部分环境下不准）。"""
    app = QApplication.instance()
    if app is not None:
        c = app.palette().color(role)
        if c.isValid():
            return c
    return widget.palette().color(role)


class WeaponCardImageArea(QWidget):
    """复用库存页 ``inventoryCardImageArea`` 等 objectName，以便 ``page_inventory.qss`` 规则生效。"""

    def __init__(
        self,
        parent=None,
        *,
        area_height: int,
        icon_width: int,
        icon_height: int,
        gradient_top_white: bool = False,
        gradient_top_palette_role: QPalette.ColorRole = QPalette.ColorRole.Window,
    ) -> None:
        super().__init__(parent)
        h = max(1, int(area_height))
        self._icon_w = max(1, int(icon_width))
        self._icon_h = max(1, int(icon_height))
        self._gradient_top_white = bool(gradient_top_white)
        self._gradient_top_palette_role = gradient_top_palette_role
        self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._line_hex, self._tint_hex = _DEFAULT_ACCENT

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._image_area = QWidget(self)
        self._image_area.setObjectName("inventoryCardImageArea")
        self._image_area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._image_area.setFixedHeight(h)

        ial = QVBoxLayout(self._image_area)
        ial.setContentsMargins(*_IMAGE_MARGINS)
        ial.setSpacing(0)

        self._icon_wrap = QWidget(self._image_area)
        self._icon_wrap.setObjectName("inventoryCardIconWrap")
        self._icon_wrap.setFixedSize(self._icon_w, self._icon_h)
        self._icon_wrap.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._icon_label = QLabel(self._icon_wrap)
        self._icon_label.setObjectName("inventoryCardIcon")
        self._icon_label.setFixedSize(self._icon_w, self._icon_h)
        self._icon_label.move(0, 0)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setScaledContents(False)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._icon_label.setAutoFillBackground(False)

        ial.addStretch(1)
        ial.addWidget(self._icon_wrap, 0, Qt.AlignmentFlag.AlignHCenter)
        ial.addStretch(1)

        self._bottom_line = QWidget(self._image_area)
        self._bottom_line.setObjectName("inventoryCardBottomLine")
        self._bottom_line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._bottom_line.setFixedHeight(_BOTTOM_LINE_H)
        ial.addWidget(self._bottom_line, 0)

        root.addWidget(self._image_area)
        QTimer.singleShot(0, self._apply_gradient_style)

    def set_quality_colors(self, line_hex: str, tint_hex: str) -> None:
        self._line_hex = line_hex
        self._tint_hex = tint_hex
        self._apply_gradient_style()

    def refresh_for_palette(self) -> None:
        self._apply_gradient_style()

    def _apply_gradient_style(self) -> None:
        top = (
            _GRADIENT_TOP_WHITE
            if self._gradient_top_white
            else _palette_role_color(self, self._gradient_top_palette_role)
        )
        tint = QColor(self._tint_hex)
        if not tint.isValid():
            tint = QColor(_DEFAULT_ACCENT[1])
        bottom_name = _grad_bottom_from_top(top, tint, _TINT_RATIO)
        mid_pos = max(0.05, min(0.95, float(_MID_STOP_POS)))
        self._image_area.setStyleSheet(
            "QWidget#inventoryCardImageArea {"
            "border-radius: 8px; border: none;"
            "background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {top.name()}, stop:{mid_pos} {top.name()}, stop:1 {bottom_name});"
            "}"
        )
        self._bottom_line.setStyleSheet(
            "QWidget#inventoryCardBottomLine {"
            f"background-color: {self._line_hex}; border: none;"
            "border-bottom-left-radius: 6px; border-bottom-right-radius: 6px;"
            "}"
        )

    def set_weapon_pixmap(self, pix: QPixmap | None) -> None:
        if pix is None or pix.isNull():
            self._icon_label.clear()
            self._icon_label.setPixmap(QPixmap())
            self._icon_label.setText("")
            return
        scaled = pix.scaled(
            self._icon_w,
            self._icon_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._icon_label.setText("")
        self._icon_label.setPixmap(scaled)

    def set_icon_placeholder_text(self, text: str) -> None:
        """无图或加载失败时在图标格内显示短文案。"""
        self._icon_label.clear()
        self._icon_label.setPixmap(QPixmap())
        self._icon_label.setWordWrap(True)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setText(text)
