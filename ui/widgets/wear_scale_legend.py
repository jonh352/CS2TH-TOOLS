"""库存卡片：磨损浮点值对应的五色刻度与向下三角指示。"""

from PySide6.QtCore import QPointF, Qt, QSize
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.data_utils import WEAR_ZONE_COLORS

# 从左到右各段占总刻度宽度的比例（合计 100%）
_WEAR_SEGMENT_FRACTIONS = (0.07, 0.08, 0.23, 0.07, 0.55)
_WEAR_SEGMENT_COLORS = WEAR_ZONE_COLORS

_TRI_H = 7
_BAR_H = 5
_MARGIN_BOTTOM = 2
_GAP_TRI_BAR = 1

_LEGEND_H = _TRI_H + _GAP_TRI_BAR + _BAR_H + _MARGIN_BOTTOM


class WearScaleLegendWidget(QWidget):
    """
    上方为向下的三角箭头（顶点抵在刻度条上沿），下方为窄矩形五色刻度条。
    箭头水平位置 = clamp(磨损值, 0, 1) * 刻度总宽度（从左算起）。
    """

    def __init__(self, wear_normalized: float, parent=None, *, bar_width: int = 220):
        super().__init__(parent)
        self.setObjectName("inventoryWearScaleLegend")
        self._wear = float(wear_normalized)
        self._bar_width = max(1, int(bar_width))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        # 与名称行同宽；AlignLeft 时布局按 sizeHint 给宽，裸 QWidget 宽为 0 会导致无法绘制
        self.setFixedSize(self._bar_width, _LEGEND_H)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(self._bar_width, _LEGEND_H)

    def minimumSizeHint(self) -> QSize:
        return QSize(self._bar_width, _LEGEND_H)

    def set_wear(self, wear_normalized: float) -> None:
        self._wear = float(wear_normalized)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        w = max(1, self.width(), self._bar_width)
        h = self.height()
        bar_y = h - _MARGIN_BOTTOM - _BAR_H
        tri_top = bar_y - _GAP_TRI_BAR - _TRI_H

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        x_left = 0
        n = len(_WEAR_SEGMENT_FRACTIONS)
        for i, (frac, hex_c) in enumerate(zip(_WEAR_SEGMENT_FRACTIONS, _WEAR_SEGMENT_COLORS)):
            if i == n - 1:
                seg_w = w - x_left
            else:
                seg_w = int(round(w * frac))
            seg_w = max(1, seg_w)
            painter.fillRect(x_left, bar_y, seg_w, _BAR_H, QColor(hex_c))
            x_left += seg_w

        t = max(0.0, min(1.0, self._wear))
        cx = t * w
        path = QPainterPath()
        path.moveTo(QPointF(cx, bar_y))
        path.lineTo(QPointF(cx - 4.5, tri_top))
        path.lineTo(QPointF(cx + 4.5, tri_top))
        path.closeSubpath()
        # 浅色背景用 Shadow（text_muted）比 WindowText 更柔和；深色仍用主文字色保证可见
        bg = self.palette().color(QPalette.ColorRole.Window)
        if bg.lightness() > 128:
            tri_color = self.palette().color(QPalette.ColorRole.Shadow)
        else:
            tri_color = self.palette().color(QPalette.ColorRole.WindowText)
        painter.setPen(QPen(tri_color, 1))
        painter.setBrush(tri_color)
        painter.drawPath(path)
