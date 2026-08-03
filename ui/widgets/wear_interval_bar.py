"""Compact wear-bucket ruler with a highlighted purchase interval."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from core.data_utils import MID_VALUE_LIST


class WearIntervalBar(QWidget):
    def __init__(
        self,
        *,
        total_min: float,
        total_max: float,
        selected_min: float,
        selected_max: float,
        marker: float | None,
        parent=None,
        recipe_min: float | None = None,
        recipe_max: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.total_min = float(total_min)
        self.total_max = float(total_max)
        self.selected_min = float(selected_min)
        self.selected_max = float(selected_max)
        self.marker = float(marker) if marker is not None else None
        self.recipe_min = float(recipe_min) if recipe_min is not None else None
        self.recipe_max = float(recipe_max) if recipe_max is not None else None
        self.setMinimumHeight(72)
        self._refresh_base_tooltip()

    def _refresh_base_tooltip(self) -> None:
        lines = [
            f"饰品总磨损 {self.total_min:g}–{self.total_max:g}",
            f"采集区间 {self.selected_min:g}–{self.selected_max:g}",
        ]
        if self.recipe_min is not None and self.recipe_max is not None:
            lines.append(f"配方磨损 {self.recipe_min:g}–{self.recipe_max:g}")
        elif self.marker is not None:
            lines.append(f"对应磨损 {self.marker:g}")
        lines.append("每一小格对应一个预设磨损档")
        self.setToolTip("\n".join(lines))

    def _anchors(self) -> list[float]:
        anchors = [self.total_min]
        anchors.extend(
            value
            for value in MID_VALUE_LIST
            if self.total_min < value < self.total_max
        )
        anchors.append(self.total_max)
        return sorted(set(anchors))

    @staticmethod
    def _anchor_position(value: float, anchors: list[float], left: float, width: float) -> float:
        if len(anchors) <= 1:
            return left
        nearest = min(range(len(anchors)), key=lambda index: abs(anchors[index] - value))
        return left + width * nearest / (len(anchors) - 1)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        anchors = self._anchors()
        left, right = 18.0, max(19.0, float(self.width()) - 18.0)
        width = right - left
        top, height = 22.0, 16.0
        base = QRectF(left, top, width, height)

        palette = self.palette()
        border = palette.color(QPalette.ColorRole.Light)
        muted = palette.color(QPalette.ColorRole.Mid)
        text = palette.color(QPalette.ColorRole.Text)
        highlight = palette.color(QPalette.ColorRole.Highlight)
        background = palette.color(QPalette.ColorRole.Base)

        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(base, 7, 7)

        selected_left = self._anchor_position(self.selected_min, anchors, left, width)
        selected_right = self._anchor_position(self.selected_max, anchors, left, width)
        selection = QRectF(
            min(selected_left, selected_right),
            top,
            abs(selected_right - selected_left),
            height,
        )
        selected_fill = QColor(highlight)
        selected_fill.setAlpha(82)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(selected_fill)
        painter.drawRect(selection)

        # Recipe wear band (like special-wear target annotation).
        recipe_left = recipe_right = None
        if self.recipe_min is not None and self.recipe_max is not None:
            recipe_left = self._anchor_position(self.recipe_min, anchors, left, width)
            recipe_right = self._anchor_position(self.recipe_max, anchors, left, width)
            recipe_fill = QColor(highlight)
            recipe_fill.setAlpha(150)
            painter.setBrush(recipe_fill)
            painter.drawRect(
                QRectF(
                    min(recipe_left, recipe_right),
                    top + 2,
                    max(2.0, abs(recipe_right - recipe_left)),
                    height - 4,
                )
            )
            painter.setPen(QPen(highlight, 2))
            for x in (recipe_left, recipe_right):
                painter.drawLine(
                    QPointF(x, top - 1),
                    QPointF(x, top + height + 1),
                )

        for index in range(1, len(anchors) - 1):
            x = left + width * index / (len(anchors) - 1)
            active = selected_left <= x <= selected_right
            painter.setPen(QPen(highlight if active else muted, 1.2 if active else 1))
            painter.drawLine(QPointF(x, top + 2), QPointF(x, top + height - 2))

        painter.setPen(QPen(highlight, 2))
        for x in (selected_left, selected_right):
            painter.drawLine(QPointF(x, top - 3), QPointF(x, top + height + 3))
            triangle = QPolygonF(
                (
                    QPointF(x - 5, top - 8),
                    QPointF(x + 5, top - 8),
                    QPointF(x, top - 2),
                )
            )
            painter.setBrush(highlight)
            painter.drawPolygon(triangle)

        if self.marker is not None:
            marker_x = self._anchor_position(self.marker, anchors, left, width)
            painter.setPen(QPen(highlight, 2))
            painter.setBrush(highlight)
            painter.drawEllipse(QPointF(marker_x, top + height / 2), 3.2, 3.2)

        painter.setPen(text)
        font = painter.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        painter.setFont(font)
        # Handle labels = collection range; bar ends = skin total wear.
        painter.setPen(highlight)
        label_w = 52.0
        for value, x in (
            (self.selected_min, selected_left),
            (self.selected_max, selected_right),
        ):
            painter.drawText(
                QRectF(x - label_w / 2, top + height + 4, label_w, 16),
                Qt.AlignmentFlag.AlignHCenter,
                f"{value:g}",
            )
        painter.setPen(muted)
        painter.drawText(
            QRectF(left, top + height + 20, width / 2, 16),
            Qt.AlignmentFlag.AlignLeft,
            f"{self.total_min:g}",
        )
        painter.drawText(
            QRectF(left + width / 2, top + height + 20, width / 2, 16),
            Qt.AlignmentFlag.AlignRight,
            f"{self.total_max:g}",
        )
        if (
            self.recipe_min is not None
            and self.recipe_max is not None
            and recipe_left is not None
            and recipe_right is not None
        ):
            painter.setPen(highlight)
            mid_x = (min(recipe_left, recipe_right) + max(recipe_left, recipe_right)) / 2
            painter.drawText(
                QRectF(mid_x - 54, top - 16, 108, 14),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                f"配方 {self.recipe_min:g}–{self.recipe_max:g}",
            )


class WearRangeSelector(WearIntervalBar):
    """Two-handle wear selector snapping to the visible bucket boundaries."""

    rangeChanged = Signal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(
            total_min=0.0,
            total_max=1.0,
            selected_min=0.0,
            selected_max=1.0,
            marker=None,
            parent=parent,
        )
        self._drag_handle: str | None = None
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setMinimumHeight(84)
        self._refresh_tooltip()

    def set_wear_bounds(
        self,
        total_min: float,
        total_max: float,
        *,
        selected_min: float | None = None,
        selected_max: float | None = None,
    ) -> None:
        self.total_min = float(total_min)
        self.total_max = float(total_max)
        self.selected_min = (
            self.total_min if selected_min is None else float(selected_min)
        )
        self.selected_max = (
            self.total_max if selected_max is None else float(selected_max)
        )
        self._normalize_selection()
        self._refresh_tooltip()
        self.update()

    def set_recipe_annotation(
        self,
        *,
        recipe_min: float | None = None,
        recipe_max: float | None = None,
        marker: float | None = None,
    ) -> None:
        """Mark the recipe float band / point, like special-wear target annotation."""
        self.recipe_min = float(recipe_min) if recipe_min is not None else None
        self.recipe_max = float(recipe_max) if recipe_max is not None else None
        if (
            self.recipe_min is not None
            and self.recipe_max is not None
            and self.recipe_min > self.recipe_max
        ):
            self.recipe_min, self.recipe_max = self.recipe_max, self.recipe_min
        self.marker = float(marker) if marker is not None else None
        self._refresh_tooltip()
        self.update()

    def selected_range(self) -> tuple[float, float]:
        return self.selected_min, self.selected_max

    def _normalize_selection(self) -> None:
        anchors = self._anchors()
        if len(anchors) < 2:
            return
        low_index = min(
            range(len(anchors)),
            key=lambda index: abs(anchors[index] - self.selected_min),
        )
        high_index = min(
            range(len(anchors)),
            key=lambda index: abs(anchors[index] - self.selected_max),
        )
        low_index = min(low_index, len(anchors) - 2)
        high_index = max(high_index, low_index + 1)
        self.selected_min = anchors[low_index]
        self.selected_max = anchors[high_index]

    def _refresh_tooltip(self) -> None:
        lines = [
            f"饰品总磨损 {self.total_min:g}–{self.total_max:g}",
            f"当前采集 {self.selected_min:g}–{self.selected_max:g}",
        ]
        if self.recipe_min is not None and self.recipe_max is not None:
            lines.append(f"配方磨损 {self.recipe_min:g}–{self.recipe_max:g}")
        elif self.marker is not None:
            lines.append(f"对应磨损 {self.marker:g}")
        lines.append("拖动左右手柄选择区间，手柄会吸附到磨损档边界")
        self.setToolTip("\n".join(lines))

    def _index_at_x(self, x: float) -> int:
        anchors = self._anchors()
        left, right = 18.0, max(19.0, float(self.width()) - 18.0)
        ratio = max(0.0, min(1.0, (x - left) / (right - left)))
        return max(
            0,
            min(len(anchors) - 1, round(ratio * (len(anchors) - 1))),
        )

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return super().mousePressEvent(event)
        anchors = self._anchors()
        index = self._index_at_x(event.position().x())
        low_index = anchors.index(self.selected_min)
        high_index = anchors.index(self.selected_max)
        self._drag_handle = (
            "low"
            if abs(index - low_index) <= abs(index - high_index)
            else "high"
        )
        self._move_handle(index)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_handle is None:
            return super().mouseMoveEvent(event)
        self._move_handle(self._index_at_x(event.position().x()))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_handle = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _move_handle(self, index: int) -> None:
        anchors = self._anchors()
        low_index = anchors.index(self.selected_min)
        high_index = anchors.index(self.selected_max)
        if self._drag_handle == "low":
            low_index = min(index, high_index - 1)
        else:
            high_index = max(index, low_index + 1)
        new_low, new_high = anchors[low_index], anchors[high_index]
        if (new_low, new_high) == (self.selected_min, self.selected_max):
            return
        self.selected_min, self.selected_max = new_low, new_high
        self._refresh_tooltip()
        self.update()
        self.rangeChanged.emit(new_low, new_high)
