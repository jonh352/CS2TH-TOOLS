"""数据采集页：自定义采集单条条目（皮肤名 + 品质标签 + 磨损双端条）。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.data_utils import QUALITY_COLORS, SkinTemplate
from ui.widgets.mid_wear_range_slider import MidWearRangeSlider

# QLineEdit 左侧 4px（见 QSS）；名称与品质之间由左组 layout 控制；此处为防裁字的少量余量
_SKIN_EDIT_PAD_H = 10


class FetchCustomEntryRow(QWidget):
    """左侧：顶行文案居中 + 全宽磨损条；右侧：删除键相对整块上下居中。"""

    delete_requested = Signal()

    def __init__(self, skin_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("fetchCustomEntryRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(10)

        left_wrap = QWidget(self)
        left_wrap.setObjectName("fetchCustomEntryLeftWrap")
        left_wrap.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        left_lay = QVBoxLayout(left_wrap)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(0)

        self.skin_edit = QLineEdit(self)
        self.skin_edit.setObjectName("fetchCustomEntrySkinEdit")
        self.skin_edit.setReadOnly(True)
        self.skin_edit.setFrame(False)
        self.skin_edit.setPlaceholderText("皮肤名称")
        self.skin_edit.setMinimumHeight(32)
        self.skin_edit.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self.skin_edit.setText(skin_name)
        self.skin_edit.textChanged.connect(self._sync_skin_display_width)
        self.skin_edit.installEventFilter(self)
        self._sync_skin_display_width()

        self.quality_label = QLabel("", self)
        self.quality_label.setObjectName("fetchCustomQualityTag")
        self.quality_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.quality_label.setVisible(False)

        self._wear_float_desc = QLabel("", self)
        self._wear_float_desc.setObjectName("fetchCustomWearFloatDesc")
        self._wear_float_desc.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._wear_float_desc.setWordWrap(False)
        self._wear_float_desc.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        self._wear_interval_desc = QLabel("", self)
        self._wear_interval_desc.setObjectName("fetchCustomWearIntervalDesc")
        self._wear_interval_desc.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._wear_interval_desc.setWordWrap(False)
        self._wear_interval_desc.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        # 顶行两段磨损文案分两个 QLabel，竖线须单独控件（slider.range_display_text 不参与本行绘制）
        self._wear_sep_label = QLabel("\u2502", self)
        self._wear_sep_label.setObjectName("fetchCustomWearSepLabel")
        self._wear_sep_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self._wear_sep_label.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )

        cluster = QWidget(self)
        cluster.setObjectName("fetchCustomEntryTopCluster")
        cluster.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        cluster_lay = QHBoxLayout(cluster)
        cluster_lay.setContentsMargins(0, 0, 0, 0)
        cluster_lay.setSpacing(4)
        cluster_lay.addWidget(self.skin_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        cluster_lay.addWidget(self.quality_label, 0, Qt.AlignmentFlag.AlignVCenter)
        cluster_lay.addWidget(self._wear_float_desc, 0, Qt.AlignmentFlag.AlignVCenter)
        cluster_lay.addWidget(self._wear_sep_label, 0, Qt.AlignmentFlag.AlignVCenter)
        cluster_lay.addWidget(self._wear_interval_desc, 0, Qt.AlignmentFlag.AlignVCenter)

        self._apply_wear_desc_label_font()

        self.wear_slider = MidWearRangeSlider(self)
        self.wear_slider.set_paint_range_label(False)
        self.wear_slider.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.wear_slider.range_changed.connect(self._sync_wear_range_desc_label)

        top.addStretch(1)
        top.addWidget(
            cluster,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
        )
        top.addStretch(1)

        left_lay.addLayout(top)
        left_lay.addWidget(self.wear_slider, 0)

        self._remove_btn = QPushButton("×", self)
        self._remove_btn.setObjectName("fetchCustomEntryRemoveBtn")
        self._remove_btn.setFixedSize(28, 28)
        self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_btn.setToolTip("移除此条")
        self._remove_btn.setAutoDefault(False)
        self._remove_btn.setDefault(False)
        self._remove_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._remove_btn.clicked.connect(self.delete_requested.emit)

        root.addWidget(left_wrap, 1)
        root.addWidget(
            self._remove_btn,
            0,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
        )
        self._sync_wear_range_desc_label()

    def _apply_wear_desc_label_font(self) -> None:
        """与 ``MidWearRangeSlider`` 内原底部文案字号一致。"""
        lf = QFont(self.font())
        ps = lf.pointSizeF()
        if ps > 0:
            lf.setPointSizeF(ps + 1.25)
        else:
            px = lf.pixelSize()
            lf.setPixelSize(max(12, px + 2) if px > 0 else 13)
        self._wear_float_desc.setFont(lf)
        self._wear_sep_label.setFont(lf)
        self._wear_interval_desc.setFont(lf)

    def _sync_wear_range_desc_label(self, *_args: object) -> None:
        self._wear_float_desc.setText(self.wear_slider.range_display_wear_span_text())
        self._wear_interval_desc.setText(self.wear_slider.range_display_interval_count_text())

    def _sync_skin_display_width(self) -> None:
        """按当前字体测量整行文本宽度，避免名称被裁切或省略。"""
        text = (self.skin_edit.text() or "").strip()
        if not text:
            text = (self.skin_edit.placeholderText() or "").strip() or "皮肤名称"
        fm = self.skin_edit.fontMetrics()
        w = max(fm.horizontalAdvance(text), fm.boundingRect(text).width())
        self.skin_edit.setFixedWidth(max(96, w + _SKIN_EDIT_PAD_H))
        self.skin_edit.updateGeometry()
        self.updateGeometry()

    def set_skin_name(self, name: str) -> None:
        self.skin_edit.setText(name or "")

    def skin_name(self) -> str:
        return self.skin_edit.text().strip()

    def set_quality_from_template(
        self, t: SkinTemplate | None, *, neighbor_wear: float | None = None
    ) -> None:
        """模板命中时品质唯一确定：QUALITY_COLORS 实心底块，略大于炼金表头小标签。

        ``neighbor_wear`` 非空时在同一轮内设置「目标磨损 ± 相邻档」区间，并只刷新一次磨损文案（避免连续两次 ``range_changed``）。
        """
        q = (t.quality or "").strip() if t is not None else ""
        if q and q in QUALITY_COLORS:
            self.quality_label.setText(q)
            bg, fg = QUALITY_COLORS[q]
            self.quality_label.setStyleSheet(
                f"background: {bg}; color: {fg}; "
                f"padding: 4px 6px 4px 4px; "
                f"border-radius: 6px; font-size: 13px; font-weight: 600; "
                f"letter-spacing: 0.02em; border: none;"
            )
            self.quality_label.setVisible(True)
        else:
            self.quality_label.setText("")
            self.quality_label.setStyleSheet("")
            self.quality_label.setVisible(False)

        self.wear_slider.blockSignals(True)
        try:
            if t is not None:
                self.wear_slider.set_wear_bounds(t.min_float, t.max_float)
                if neighbor_wear is not None:
                    self.wear_slider.set_span_covering_wear_with_neighbor_intervals(
                        neighbor_wear
                    )
            else:
                self.wear_slider.set_wear_bounds(0.0, 1.0)
        finally:
            self.wear_slider.blockSignals(False)
        self._sync_wear_range_desc_label()

    def quality(self) -> str:
        return self.quality_label.text().strip() if self.quality_label.isVisible() else ""

    def wear_index_range(self) -> tuple[int, int]:
        return self.wear_slider.index_range()

    def wear_float_range(self) -> tuple[float, float]:
        return self.wear_slider.wear_float_range()

    def apply_saved_wear_floats(self, wear_lo: float, wear_hi: float) -> None:
        """在 ``set_quality_from_template`` 之后调用，恢复保存的磨损区间。"""
        self.wear_slider.set_selection_from_wear_floats(wear_lo, wear_hi)

    def set_read_only(self, ro: bool) -> None:
        """详情展示：禁用磨损条与删除键。"""
        self.wear_slider.setEnabled(not ro)
        self._remove_btn.setVisible(not ro)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.FontChange:
            self._sync_skin_display_width()
            self._apply_wear_desc_label_font()
        super().changeEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.skin_edit and event.type() == QEvent.Type.FontChange:
            self._sync_skin_display_width()
        return super().eventFilter(watched, event)
