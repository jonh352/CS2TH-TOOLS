"""数据采集自定义条目：双端离散磨损条（锚点见 core.data_utils.MID_VALUE_LIST；轨道按 0~1 实际磨损比例）。"""

from __future__ import annotations

import bisect
import math
from typing import Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPalette, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from config import SLIDER_HANDLE_SVG_PATH
from ui.asset_cache import cached_shared_qsvg_renderer
from core.data_utils import LARGE_VALUE_LIST, MID_VALUE_LIST, WEAR_ZONE_COLORS

_N = len(MID_VALUE_LIST)  # 26
_LAST = _N - 1

# 与库存五色条一致：0–0.07、0.07–0.15、…、0.45–1.0（边界同 LARGE_VALUE_LIST）
_WEAR_ZONE_LO = (0.0,) + tuple(LARGE_VALUE_LIST[:-1])
_WEAR_ZONE_HI = tuple(LARGE_VALUE_LIST)


def _fmt_wear(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _nearest_slot_index(seq: Sequence[float | int], ref: float | int) -> int:
    rf = float(ref)
    return min(range(len(seq)), key=lambda k: abs(float(seq[k]) - rf))


def _fix_overlap_positions(p_lo: int, p_hi: int, n: int, pivot: str | None) -> tuple[int, int]:
    """有序列表下标；p_hi <= p_lo 时按 pivot 拉开至少一档（与 MidWearRangeSlider._enforce_min_handle_separation 一致）。"""
    if p_hi > p_lo:
        return p_lo, p_hi
    if pivot == "min":
        if p_hi > 0:
            p_lo = p_hi - 1
        elif p_lo + 1 < n:
            p_hi = p_lo + 1
    elif pivot == "max":
        if p_lo + 1 < n:
            p_hi = p_lo + 1
        elif p_hi > 0:
            p_lo = p_hi - 1
    else:
        if p_hi > 0:
            p_lo = p_hi - 1
        elif p_lo + 1 < n:
            p_hi = p_lo + 1
        else:
            p_lo = p_hi - 1
    return p_lo, p_hi


class MidWearRangeSlider(QWidget):
    """轨道宽度按全局磨损 0~1 线性比例；取值仍为离散 MID（及非全量时的边界）。
    非全量皮肤可选值为 skin 上下界 + 区间内 MID，以便 max_float=0.75 等能精确选中。"""

    range_changed = Signal(int, int)  # min_idx, max_idx（最近锚点，兼容旧接口）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("fetchMidWearRangeSlider")
        self._paint_range_label = True
        self.setMinimumHeight(52)
        self.setMinimumWidth(180)
        self._skin_lo = 0.0
        self._skin_hi = 1.0
        self._min_wear = float(MID_VALUE_LIST[0])
        self._max_wear = float(MID_VALUE_LIST[_LAST])
        self._min_i = 0
        self._max_i = _LAST
        self._drag: str | None = None
        # 与 range_changed 同步：仅用 MID 下标会漏发（非全量皮肤下磨损/区间数可变但最近 MID 不变）
        self._last_range_display_emit: str | None = None
        self._handle_pixmap: QPixmap | None = None
        # 与 ``_skin_lo`` / ``_skin_hi`` 对应；皮肤边界未变时复用，避免 ``set_wear_bounds`` 与 ``set_span`` 重复排序
        self._snap_tuple_cand: tuple[float, ...] | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_paint_range_label(self, show: bool) -> None:
        """是否在控件底部绘制「磨损度 / 区间数量」文案；关闭时可配合外部 QLabel 展示。"""
        show = bool(show)
        if self._paint_range_label == show:
            return
        self._paint_range_label = show
        if show:
            self.setMinimumHeight(52)
            self.setMinimumWidth(180)
        else:
            self.setMinimumHeight(self._compact_paint_min_height())
            # 无底部文案时宽度随父布局拉满，仅保留轨道可点的下限
            self.setMinimumWidth(80)
        self.updateGeometry()
        self.update()

    def range_display_wear_span_text(self) -> str:
        """磨损区间端点展示（与 ``range_display_text`` 前半段一致）。"""
        lo_v, hi_v = self.wear_float_range()
        return f"已选择磨损度范围：{_fmt_wear(lo_v)} ~ {_fmt_wear(hi_v)}"

    def range_display_interval_count_text(self) -> str:
        """区间数量展示（与 ``range_display_text`` 后半段一致）。"""
        return f"区间数量：{self._interval_count_for_display()}"

    def range_display_text(self) -> str:
        """与 ``paintEvent`` 底部文案一致（U+2502 竖线；拆多 QLabel 的界面须另放分隔控件）。"""
        sep = "|"
        return (
            f"{self.range_display_wear_span_text()} {sep} {self.range_display_interval_count_text()}"
        )

    def _compact_paint_min_height(self) -> int:
        """无底部文案时：针尖在 y=手柄高，上方需完整图标；下方刻度线 + 少量留白。"""
        sz = float(self._handle_icon_px())
        tick_h = 5.0
        pad_bottom = 5.0
        return max(30, int(math.ceil(sz + tick_h + pad_bottom)))

    def sizeHint(self) -> QSize:
        if self._paint_range_label:
            return QSize(300, 56)
        return QSize(160, self._compact_paint_min_height())

    def _is_full_skin_range(self) -> bool:
        return abs(self._skin_lo) < 1e-9 and abs(self._skin_hi - 1.0) < 1e-9

    def _sync_indices_from_wear(self) -> None:
        self._min_i = self._nearest_index_for_float(self._min_wear)
        self._max_i = self._nearest_index_for_float(self._max_wear)

    def _nearest_index_for_float(self, v: float) -> int:
        return min(range(_N), key=lambda i: abs(MID_VALUE_LIST[i] - v))

    def _sorted_snap_tuple(self) -> tuple[float, ...]:
        if self._snap_tuple_cand is not None:
            return self._snap_tuple_cand
        self._snap_tuple_cand = tuple(sorted(self._snap_candidates()))
        return self._snap_tuple_cand

    def set_span_covering_wear_with_neighbor_intervals(self, wear: float) -> None:
        """将双端范围设为包含 ``wear`` 的相邻档段，并并入其前一档与后一档（与 ``_snap_candidates`` 离散段一致）。"""
        wear = max(self._skin_lo, min(self._skin_hi, float(wear)))
        prev = (self._min_i, self._max_i)
        cand = self._sorted_snap_tuple()
        n = len(cand)
        if n < 2:
            self._notify_range_update(prev)
            return
        wear_c = max(cand[0], min(cand[-1], wear))
        j = bisect.bisect_right(cand, wear_c + 1e-12)
        k = min(n - 2, max(0, j - 1))
        lo_i = max(0, k - 1)
        hi_i = min(n - 1, k + 2)
        self._min_wear = float(cand[lo_i])
        self._max_wear = float(cand[hi_i])
        if self._min_wear > self._max_wear:
            self._min_wear, self._max_wear = self._max_wear, self._min_wear
        # 全量皮肤时 _enforce_min_handle_separation 用 _min_i/_max_i 映射到 valid 档；
        # 若不先与 _min_wear/_max_wear 对齐，会沿用 set_wear_bounds 留下的宽区间，把刚设的窄范围覆盖成「全选」。
        self._sync_indices_from_wear()
        self._enforce_min_handle_separation()
        self._notify_range_update(prev)

    def set_index_range(self, lo: int, hi: int) -> None:
        lo = max(0, min(_LAST, int(lo)))
        hi = max(0, min(_LAST, int(hi)))
        if lo > hi:
            lo, hi = hi, lo
        self._min_wear = float(MID_VALUE_LIST[lo])
        self._max_wear = float(MID_VALUE_LIST[hi])
        if self._min_wear > self._max_wear:
            self._min_wear, self._max_wear = self._max_wear, self._min_wear
        if not self._is_full_skin_range():
            self._min_wear = self._snap_linear_wear(self._min_wear)
            self._max_wear = self._snap_linear_wear(self._max_wear)
            if self._min_wear > self._max_wear:
                self._min_wear, self._max_wear = self._max_wear, self._min_wear
        prev = (self._min_i, self._max_i)
        self._enforce_min_handle_separation()
        self._notify_range_update(prev)

    def index_range(self) -> tuple[int, int]:
        return (self._min_i, self._max_i)

    def wear_float_range(self) -> tuple[float, float]:
        lo, hi = self._min_wear, self._max_wear
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    def set_wear_bounds(self, lo: float, hi: float) -> None:
        lo = max(0.0, min(1.0, float(lo)))
        hi = max(0.0, min(1.0, float(hi)))
        if lo > hi:
            lo, hi = hi, lo
        self._skin_lo = lo
        self._skin_hi = hi
        self._snap_tuple_cand = None
        prev = (self._min_i, self._max_i)
        if self._is_full_skin_range():
            valid = self._valid_indices()
            if not valid:
                self._min_i, self._max_i = 0, _LAST
            else:
                self._min_i = min(valid, key=lambda i: abs(MID_VALUE_LIST[i] - lo))
                self._max_i = min(valid, key=lambda i: abs(MID_VALUE_LIST[i] - hi))
                if self._min_i > self._max_i:
                    self._min_i, self._max_i = self._max_i, self._min_i
            self._min_wear = float(MID_VALUE_LIST[self._min_i])
            self._max_wear = float(MID_VALUE_LIST[self._max_i])
        else:
            cand = list(self._sorted_snap_tuple())
            self._min_wear = min(cand, key=lambda c: abs(c - lo))
            self._max_wear = min(cand, key=lambda c: abs(c - hi))
            if self._min_wear > self._max_wear:
                self._min_wear, self._max_wear = self._max_wear, self._min_wear
        self._enforce_min_handle_separation()
        self._notify_range_update(prev)

    def set_selection_from_wear_floats(self, lo: float, hi: float) -> None:
        """在已由 ``set_wear_bounds`` / 模板设好皮肤可磨损区间后，按保存的实数端点恢复选中区间（snap 到候选）。"""
        lo_v = self._snap_linear_wear(max(self._skin_lo, min(self._skin_hi, float(lo))))
        hi_v = self._snap_linear_wear(max(self._skin_lo, min(self._skin_hi, float(hi))))
        if lo_v > hi_v:
            lo_v, hi_v = hi_v, lo_v
        prev = (self._min_i, self._max_i)
        self._min_wear = lo_v
        self._max_wear = hi_v
        self._sync_indices_from_wear()
        self._enforce_min_handle_separation()
        self._notify_range_update(prev)

    def _snap_candidates(self) -> list[float]:
        s = {self._skin_lo, self._skin_hi}
        for i in range(_N):
            v = MID_VALUE_LIST[i]
            if self._skin_lo - 1e-9 <= v <= self._skin_hi + 1e-9:
                s.add(v)
        return sorted(s)

    def _snap_linear_wear(self, v: float) -> float:
        v = max(self._skin_lo, min(self._skin_hi, float(v)))
        cand = self._snap_candidates()
        return min(cand, key=lambda c: abs(c - v))

    def _notify_range_update(self, prev: tuple[int, int]) -> None:
        self.update()
        cur_text = self.range_display_text()
        idx_changed = (self._min_i, self._max_i) != prev
        # 文案依赖 wear_float_range + _interval_count_for_display，与「最近 MID 下标」不完全一致
        text_changed = (
            self._last_range_display_emit is None or cur_text != self._last_range_display_emit
        )
        if idx_changed or text_changed:
            self._last_range_display_emit = cur_text
            self.range_changed.emit(self._min_i, self._max_i)

    def _enforce_min_handle_separation(self, pivot: str | None = None) -> None:
        """两滑块至少相隔一个可选档（候选列表中相邻两值），避免重合无法点选。

        pivot: 拖动时只约束被拖的一侧，避免左拖把右推开 / 右拖把左推开。
        - min: 固定右端，左端最多到右端前一档
        - max: 固定左端，右端至少为左端后一档
        - None: 程序设值时优先左移左端，否则右移右端
        """
        if self._is_full_skin_range():
            valid = sorted(self._valid_indices())
            if len(valid) < 2:
                return
            n = len(valid)
            p_lo = _nearest_slot_index(valid, self._min_i)
            p_hi = _nearest_slot_index(valid, self._max_i)
            p_lo, p_hi = _fix_overlap_positions(p_lo, p_hi, n, pivot)
            self._min_i = valid[p_lo]
            self._max_i = valid[p_hi]
            self._min_wear = float(MID_VALUE_LIST[self._min_i])
            self._max_wear = float(MID_VALUE_LIST[self._max_i])
        else:
            cand = list(self._sorted_snap_tuple())
            if len(cand) < 2:
                return
            n = len(cand)
            p_lo = _nearest_slot_index(cand, self._min_wear)
            p_hi = _nearest_slot_index(cand, self._max_wear)
            p_lo, p_hi = _fix_overlap_positions(p_lo, p_hi, n, pivot)
            self._min_wear = float(cand[p_lo])
            self._max_wear = float(cand[p_hi])
            self._sync_indices_from_wear()

    def _apply_drag_at_x(self, x: float, w: int) -> None:
        if self._is_full_skin_range():
            wear = self._wear_at_linear_x(x, w)
            valid = self._valid_indices()
            idx = min(valid, key=lambda i: abs(MID_VALUE_LIST[i] - wear))
            if self._drag == "min":
                ni = min(idx, self._max_i)
                if ni != self._min_i:
                    self._min_i = ni
                    self._min_wear = float(MID_VALUE_LIST[self._min_i])
            else:
                ni = max(idx, self._min_i)
                if ni != self._max_i:
                    self._max_i = ni
                    self._max_wear = float(MID_VALUE_LIST[self._max_i])
        else:
            snapped = self._snap_linear_wear(self._wear_at_linear_x(x, w))
            if self._drag == "min":
                nv = min(snapped, self._max_wear)
                if abs(nv - self._min_wear) > 1e-12:
                    self._min_wear = nv
            else:
                nv = max(snapped, self._min_wear)
                if abs(nv - self._max_wear) > 1e-12:
                    self._max_wear = nv

    def _finalize_drag_step(self, prev: tuple[int, int]) -> None:
        self._enforce_min_handle_separation(self._drag)
        self._notify_range_update(prev)

    def _track_margin(self) -> float:
        return 14.0

    def _usable_width(self, w: int) -> tuple[float, float]:
        m = self._track_margin()
        usable = max(1.0, float(w) - 2.0 * m)
        return m, usable

    def _x_linear(self, fv: float, w: int) -> float:
        """全局磨损 [0,1] 在整条轨道上按实际比例取 x（全量 / 非全量统一）。"""
        fv = max(0.0, min(1.0, float(fv)))
        m, usable = self._usable_width(w)
        return m + fv * usable

    def _wear_at_linear_x(self, x: float, w: int) -> float:
        m, usable = self._usable_width(w)
        return max(0.0, min(1.0, (float(x) - m) / usable))

    def _x_for_display(self, fv: float, w: int) -> float:
        return self._x_linear(fv, w)

    def _x_at_handle_wear(self, wear: float, w: int) -> float:
        return self._x_for_display(wear, w)

    def _valid_indices(self) -> list[int]:
        lo, hi = self._skin_lo, self._skin_hi
        out = [i for i in range(_N) if lo - 1e-9 <= MID_VALUE_LIST[i] <= hi + 1e-9]
        return out if out else list(range(_N))

    def _interval_count_for_display(self) -> int:
        """全量皮肤：全局 MID 下标差。非全量：本皮肤 ``_snap_candidates()`` 上两端下标差。

        避免 max 落在 skin_hi（如 0.8）等非 MID 锚点时，``_nearest_index_for_float`` 把两端都指到同一 MID（如 0.76）导致差为 0。
        """
        if self._is_full_skin_range():
            lo_i, hi_i = sorted((self._min_i, self._max_i))
            return max(0, hi_i - lo_i)
        cand = list(self._sorted_snap_tuple())
        if len(cand) < 2:
            return 0
        lo_v, hi_v = sorted((self._min_wear, self._max_wear))

        def _idx(v: float) -> int:
            for i, c in enumerate(cand):
                if abs(c - v) < 1e-9:
                    return i
            return min(range(len(cand)), key=lambda i: abs(cand[i] - v))

        p_lo = _idx(lo_v)
        p_hi = _idx(hi_v)
        if p_lo > p_hi:
            p_lo, p_hi = p_hi, p_lo
        return max(0, p_hi - p_lo)

    def _handle_radius(self) -> float:
        return 6.0

    def _handle_icon_px(self) -> int:
        """滑块图标逻辑边长（与 SVG 绘制一致，亦用于命中区估算）。"""
        return 20

    def _handle_icon_top_y(self, track_y: float) -> float:
        """SVG 菱形针尖在 viewBox 底侧，使底边落在轨道上（原先中心对齐则尖角在轨道下方）。"""
        return float(track_y) - float(self._handle_icon_px())

    def _ensure_handle_svg_renderer(self) -> QSvgRenderer | None:
        """直接使用 assets 内 SVG 的填色（#DEDEDE / #474747），不按主题改色。"""
        renderer = cached_shared_qsvg_renderer(SLIDER_HANDLE_SVG_PATH)
        if renderer is None:
            self._handle_pixmap = None
        return renderer

    def _paint_handle_icons(
        self, p: QPainter, track_y: float, x_lo: float, x_hi: float
    ) -> None:
        renderer = self._ensure_handle_svg_renderer()
        sz = self._handle_icon_px()
        half = 0.5 * float(sz)
        if renderer is None:
            r = self._handle_radius()
            border = self.palette().color(QPalette.ColorRole.Mid)
            fill = self.palette().color(QPalette.ColorRole.Base)
            p.setPen(QPen(border))
            p.setBrush(fill)
            for xi in (x_lo, x_hi):
                p.drawEllipse(QRectF(xi - r, track_y - r, 2.0 * r, 2.0 * r))
            return
        pm = self._handle_pixmap
        if pm is None or pm.isNull() or pm.width() != sz:
            pm = QPixmap(sz, sz)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            renderer.render(pp, QRectF(0.0, 0.0, float(sz), float(sz)))
            pp.end()
            self._handle_pixmap = pm
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        top_y = self._handle_icon_top_y(track_y)
        for xi in (x_lo, x_hi):
            p.drawPixmap(QPointF(xi - half, top_y), pm)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        w = self.width()
        x = float(event.position().x())
        xm = self._x_at_handle_wear(self._min_wear, w)
        xM = self._x_at_handle_wear(self._max_wear, w)
        d_m = abs(x - xm)
        d_M = abs(x - xM)
        if d_m <= d_M:
            self._drag = "min"
        else:
            self._drag = "max"
        prev = (self._min_i, self._max_i)
        self._apply_drag_at_x(x, w)
        self._finalize_drag_step(prev)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton) or not self._drag:
            return super().mouseMoveEvent(event)
        w = self.width()
        prev = (self._min_i, self._max_i)
        self._apply_drag_at_x(float(event.position().x()), w)
        self._finalize_drag_step(prev)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        if self._paint_range_label:
            track_y = h * 0.42
        else:
            # 与 _handle_icon_top_y 一致：针尖落在 y=手柄边长，避免按比例算得过靠上导致图标被裁切
            track_y = float(self._handle_icon_px())
        m = self._track_margin()
        track_half_h = 3.0

        sel_lo = self._min_wear
        sel_hi = self._max_wear
        if sel_lo > sel_hi:
            sel_lo, sel_hi = sel_hi, sel_lo

        pen = QPen(QColor(self.palette().color(QPalette.ColorRole.Mid)))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawLine(QPointF(m, track_y), QPointF(float(w) - m, track_y))

        p.setPen(Qt.PenStyle.NoPen)
        for zi, color_hex in enumerate(WEAR_ZONE_COLORS):
            z_lo, z_hi = _WEAR_ZONE_LO[zi], _WEAR_ZONE_HI[zi]
            seg_lo = max(z_lo, sel_lo)
            seg_hi = min(z_hi, sel_hi)
            if seg_lo >= seg_hi:
                continue
            xa = self._x_for_display(seg_lo, w)
            xb = self._x_for_display(seg_hi, w)
            rw = max(1.0, xb - xa)
            p.setBrush(QColor(color_hex))
            p.drawRect(
                QRectF(xa, track_y - track_half_h, rw, 2.0 * track_half_h)
            )

        tick_h = 5.0
        pen_tick = QPen(QColor(self.palette().color(QPalette.ColorRole.Mid)))
        pen_tick.setWidthF(1.0)
        p.setPen(pen_tick)
        if self._is_full_skin_range():
            for i in range(_N):
                x = self._x_linear(MID_VALUE_LIST[i], w)
                p.drawLine(QPointF(x, track_y), QPointF(x, track_y + tick_h))
        else:
            for fv in self._snap_candidates():
                x = self._x_linear(fv, w)
                p.drawLine(QPointF(x, track_y), QPointF(x, track_y + tick_h))

        x_lo = self._x_at_handle_wear(self._min_wear, w)
        x_hi = self._x_at_handle_wear(self._max_wear, w)

        self._paint_handle_icons(p, track_y, x_lo, x_hi)

        if not self._paint_range_label:
            return
        text = self.range_display_text()
        label_font = QFont(self.font())
        ps = label_font.pointSizeF()
        if ps > 0:
            label_font.setPointSizeF(ps + 1.25)
        else:
            px = label_font.pixelSize()
            label_font.setPixelSize(max(12, px + 2) if px > 0 else 13)
        fm_label = QFontMetrics(label_font)
        p.setFont(label_font)
        p.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        tw = fm_label.horizontalAdvance(text)
        br = QRectF(
            (w - tw) / 2.0,
            float(h) - fm_label.height() - 2.0,
            float(tw),
            float(fm_label.height()),
        )
        p.drawText(br, int(Qt.AlignmentFlag.AlignCenter), text)
