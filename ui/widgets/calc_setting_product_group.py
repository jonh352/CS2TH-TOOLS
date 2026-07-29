"""计算设置页 - 按武器箱聚合的可折叠产物组"""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QEvent, QObject, Qt

from PySide6.QtGui import QMouseEvent, QPalette

from core.data_utils import SkinTemplate

from ..icons import expand_section_triangle_icon

from .float_line_edit import WearFloatLineEditWithIeee, format_float_shortest

_DISCLOSURE_ICON_PX = 14


class _SpecialWearRowClickSelectsRadio(QObject):
    """点击行内任意控件时选中该行单选（事件过滤，不吞掉事件）。"""

    def __init__(self, radio: QRadioButton):
        super().__init__(radio)
        self._radio = radio

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self._radio.setChecked(True)
        return False


class CalcSettingProductGroup(QFrame):
    """按武器箱展示产物；scan 与 special_wear 均为 min/max 真实磨损；target 为单一目标磨损。"""

    VALID_MODES = ("scan", "target", "special_wear")

    def __init__(
        self,
        box_name: str,
        templates: list[SkinTemplate],
        mode: str,
        parent=None,
        *,
        norm_min: float = 0.0,
        norm_max: float = 1.0,
        norm_target: float = 1.0,
        special_radio_group: QButtonGroup | None = None,
        special_is_first_box: bool = False,
    ):
        super().__init__(parent)
        if mode not in self.VALID_MODES:
            raise ValueError(f"无效的 mode: {mode}，仅支持 {self.VALID_MODES}")
        if mode == "special_wear" and special_radio_group is None:
            raise ValueError("special_wear 模式须提供 special_radio_group")
        self.setObjectName("alchemyGroup")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._expanded = True
        self._mode = mode
        self._rows: list = []
        self._special_rows: list[
            tuple[
                SkinTemplate,
                QRadioButton,
                QLabel,
                QLabel,
                WearFloatLineEditWithIeee,
                QLabel,
                WearFloatLineEditWithIeee,
                QWidget,
            ]
        ] = []
        self._special_wear_peer_groups: list[CalcSettingProductGroup] | None = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.header = QFrame(self)
        self.header.setObjectName("alchemyGroupHeader")
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setFixedHeight(44)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(16, 0, 16, 0)
        header_layout.setSpacing(8)

        self.arrow_label = QLabel(self.header)
        self.arrow_label.setObjectName("alchemyGroupArrow")
        self.arrow_label.setFixedSize(_DISCLOSURE_ICON_PX, _DISCLOSURE_ICON_PX)
        header_layout.addWidget(self.arrow_label, 0, Qt.AlignVCenter)

        self.title_label = QLabel(box_name, self.header)
        self.title_label.setObjectName("alchemyGroupTitle")
        header_layout.addWidget(self.title_label, 1, Qt.AlignVCenter)

        main_layout.addWidget(self.header)

        self.content_frame = QFrame(self)
        self.content_frame.setObjectName("alchemyTableFrame")
        self.content_frame.setVisible(True)
        content_layout = QVBoxLayout(self.content_frame)
        content_layout.setContentsMargins(16, 12, 16, 12)
        content_layout.setSpacing(4)

        if self._mode == "scan":
            for template in templates:
                self._add_scan_row(content_layout, template, norm_min, norm_max)
        elif self._mode == "target":
            for template in templates:
                self._add_target_row(content_layout, template, norm_target)
        else:
            for i, template in enumerate(templates):
                is_default = special_is_first_box and i == 0
                self._add_special_wear_row(
                    content_layout,
                    template,
                    special_radio_group,
                    norm_min,
                    norm_max,
                    default_checked=is_default,
                )
            self._wire_special_wear_row_click_and_sync()

        main_layout.addWidget(self.content_frame)
        self.header.mousePressEvent = self._on_header_clicked
        self._update_disclosure_arrow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_disclosure_arrow()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.PaletteChange:
            self._update_disclosure_arrow()
        super().changeEvent(event)

    def _update_disclosure_arrow(self) -> None:
        fill = self.palette().color(QPalette.ColorRole.Shadow).name()
        icon = expand_section_triangle_icon(
            self._expanded,
            size_px=_DISCLOSURE_ICON_PX,
            fill_color=fill,
        )
        self.arrow_label.setPixmap(
            icon.pixmap(_DISCLOSURE_ICON_PX, _DISCLOSURE_ICON_PX)
        )

    def _new_product_row(self, template: SkinTemplate) -> tuple[QWidget, QHBoxLayout]:
        row = QWidget(self.content_frame)
        row.setObjectName("alchemyProductRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 8, 0, 8)
        row_layout.setSpacing(20)
        name = (
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        name_lbl = QLabel(name, row)
        name_lbl.setObjectName("alchemyProductName")
        row_layout.addWidget(name_lbl, 1, Qt.AlignVCenter)
        return row, row_layout

    def _add_scan_row(
        self, content_layout: QVBoxLayout, template: SkinTemplate, norm_min: float, norm_max: float
    ) -> None:
        mn, mx = template.min_float, template.max_float
        row, row_layout = self._new_product_row(template)
        min_lbl = QLabel("最小磨损度", row)
        min_lbl.setObjectName("alchemyProductFieldLabel")
        row_layout.addWidget(min_lbl, 0, Qt.AlignVCenter)
        min_spin = WearFloatLineEditWithIeee(mn, mx, row)
        min_spin.line_edit().setObjectName("alchemyProductEdit")
        min_spin.setValue(SkinTemplate.normalized_to_float(norm_min, mn, mx))
        min_spin.setFixedWidth(140)
        row_layout.addWidget(min_spin, 0, Qt.AlignVCenter)
        max_lbl = QLabel("最大磨损度", row)
        max_lbl.setObjectName("alchemyProductFieldLabel")
        row_layout.addWidget(max_lbl, 0, Qt.AlignVCenter)
        max_spin = WearFloatLineEditWithIeee(mn, mx, row)
        max_spin.line_edit().setObjectName("alchemyProductEdit")
        max_spin.setValue(SkinTemplate.normalized_to_float(norm_max, mn, mx))
        max_spin.setFixedWidth(140)
        row_layout.addWidget(max_spin, 0, Qt.AlignVCenter)
        content_layout.addWidget(row)
        self._rows.append((template, min_spin, max_spin))

    def _add_target_row(self, content_layout: QVBoxLayout, template: SkinTemplate, norm_target: float) -> None:
        mn, mx = template.min_float, template.max_float
        row, row_layout = self._new_product_row(template)
        target_lbl = QLabel("目标磨损度", row)
        target_lbl.setObjectName("alchemyProductFieldLabel")
        row_layout.addWidget(target_lbl, 0, Qt.AlignVCenter)
        target_edit = WearFloatLineEditWithIeee(mn, mx, row)
        target_edit.line_edit().setObjectName("alchemyProductEdit")
        target_edit.setValue(SkinTemplate.normalized_to_float(norm_target, mn, mx))
        target_edit.setFixedWidth(140)
        row_layout.addWidget(target_edit, 0, Qt.AlignVCenter)
        content_layout.addWidget(row)
        self._rows.append((template, target_edit))

    def _add_special_wear_row(
        self,
        content_layout: QVBoxLayout,
        template: SkinTemplate,
        radio_group: QButtonGroup,
        norm_min: float,
        norm_max: float,
        *,
        default_checked: bool,
    ) -> None:
        mn, mx = template.min_float, template.max_float
        row = QWidget(self.content_frame)
        row.setObjectName("alchemyProductRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 8, 0, 8)
        row_layout.setSpacing(20)
        radio = QRadioButton(row)
        radio.setObjectName("settingsCloseOption")
        radio.setText("")
        radio.setCursor(Qt.PointingHandCursor)
        radio_group.addButton(radio)
        if default_checked:
            radio.setChecked(True)
        row_layout.addWidget(radio, 0, Qt.AlignVCenter)
        name = (
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        name_lbl = QLabel(name, row)
        name_lbl.setObjectName("alchemyProductName")
        row_layout.addWidget(name_lbl, 1, Qt.AlignVCenter)
        min_lbl = QLabel("最小磨损度", row)
        min_lbl.setObjectName("alchemyProductFieldLabel")
        row_layout.addWidget(min_lbl, 0, Qt.AlignVCenter)
        min_edit = WearFloatLineEditWithIeee(mn, mx, row)
        min_edit.line_edit().setObjectName("alchemyProductEdit")
        min_edit.setValue(SkinTemplate.normalized_to_float(norm_min, mn, mx))
        min_edit.setFixedWidth(140)
        row_layout.addWidget(min_edit, 0, Qt.AlignVCenter)
        max_lbl = QLabel("最大磨损度", row)
        max_lbl.setObjectName("alchemyProductFieldLabel")
        row_layout.addWidget(max_lbl, 0, Qt.AlignVCenter)
        max_edit = WearFloatLineEditWithIeee(mn, mx, row)
        max_edit.line_edit().setObjectName("alchemyProductEdit")
        max_edit.setValue(SkinTemplate.normalized_to_float(norm_max, mn, mx))
        max_edit.setFixedWidth(140)
        row_layout.addWidget(max_edit, 0, Qt.AlignVCenter)
        content_layout.addWidget(row)
        self._special_rows.append(
            (template, radio, name_lbl, min_lbl, min_edit, max_lbl, max_edit, row)
        )

    def set_special_wear_peer_groups(self, groups: list[CalcSettingProductGroup]) -> None:
        """特殊磨损：与所有武器箱分组共享，用于跨组同步归一化 min/max。"""
        self._special_wear_peer_groups = groups

    def _wire_special_wear_row_click_and_sync(self) -> None:
        if self._mode != "special_wear":
            return
        for tpl, radio, name_lbl, min_lbl, min_edit, max_lbl, max_edit, row in self._special_rows:
            row.setCursor(Qt.PointingHandCursor)
            filt = _SpecialWearRowClickSelectsRadio(radio)
            for w in (
                row,
                radio,
                name_lbl,
                min_lbl,
                max_lbl,
                min_edit,
                max_edit,
                min_edit.line_edit(),
                max_edit.line_edit(),
            ):
                w.installEventFilter(filt)
            min_edit.valueChanged.connect(
                lambda v, t=tpl, r=radio: self._on_special_wear_min_committed(v, t, r)
            )
            max_edit.valueChanged.connect(
                lambda v, t=tpl, r=radio: self._on_special_wear_max_committed(v, t, r)
            )

    def _special_wear_all_groups(self) -> list[CalcSettingProductGroup]:
        g = self._special_wear_peer_groups
        return g if g else [self]

    def _propagate_norm_min_to_all_rows(self, n: float) -> None:
        groups = self._special_wear_all_groups()
        edits: list[WearFloatLineEditWithIeee] = []
        for grp in groups:
            for _tpl, _r, _nl, _ml, min_e, _xl, _max_e, _rw in grp._special_rows:
                edits.append(min_e)
        for e in edits:
            e.blockSignals(True)
        try:
            for grp in groups:
                for tpl, _r, _nl, _ml, min_e, _xl, _max_e, _rw in grp._special_rows:
                    v = SkinTemplate.normalized_to_float(n, tpl.min_float, tpl.max_float)
                    min_e.setValue(v)
        finally:
            for e in edits:
                e.blockSignals(False)

    def _propagate_norm_max_to_all_rows(self, n: float) -> None:
        groups = self._special_wear_all_groups()
        edits: list[WearFloatLineEditWithIeee] = []
        for grp in groups:
            for _tpl, _r, _nl, _ml, _min_e, _xl, max_e, _rw in grp._special_rows:
                edits.append(max_e)
        for e in edits:
            e.blockSignals(True)
        try:
            for grp in groups:
                for tpl, _r, _nl, _ml, _min_e, _xl, max_e, _rw in grp._special_rows:
                    v = SkinTemplate.normalized_to_float(n, tpl.min_float, tpl.max_float)
                    max_e.setValue(v)
        finally:
            for e in edits:
                e.blockSignals(False)

    def _on_special_wear_min_committed(
        self, v: float, tpl: SkinTemplate, radio: QRadioButton
    ) -> None:
        if not radio.isChecked():
            return
        n = SkinTemplate.float_to_normalized(v, tpl.min_float, tpl.max_float)
        self._propagate_norm_min_to_all_rows(n)

    def _on_special_wear_max_committed(
        self, v: float, tpl: SkinTemplate, radio: QRadioButton
    ) -> None:
        if not radio.isChecked():
            return
        n = SkinTemplate.float_to_normalized(v, tpl.min_float, tpl.max_float)
        self._propagate_norm_max_to_all_rows(n)

    def update_special_wear_row_active_state(self) -> None:
        if self._mode != "special_wear":
            return
        for _tpl, radio, name_lbl, _min_lbl, min_edit, _max_lbl, max_edit, row in self._special_rows:
            on = radio.isChecked()
            # 勿对 QLabel 使用 setEnabled(False)：会覆盖 QSS 颜色，导致选中和未选中难以区分
            # Qt 样式表对 [inactive="false"] 匹配不可靠；选中行应「去掉」inactive，灰显规则才失效
            if on:
                row.setProperty("inactive", None)
                row.setProperty("sw_selected", True)
            else:
                row.setProperty("inactive", True)
                row.setProperty("sw_selected", None)
            # 特殊磨损模式下 min/max 仅展示自动推导结果，统一禁止手动改写。
            min_edit.setReadOnly(True)
            max_edit.setReadOnly(True)
            fp = Qt.FocusPolicy.StrongFocus if on else Qt.FocusPolicy.NoFocus
            min_edit.setFocusPolicy(fp)
            max_edit.setFocusPolicy(fp)
            if not on:
                min_edit.clearFocus()
                max_edit.clearFocus()
            # 选中行：蓝框 + 正文色；未选中：默认 mid 边框 + 灰色字（inactive_field 仅改 QSS 字色）
            for le in (min_edit.line_edit(), max_edit.line_edit()):
                if on:
                    le.setProperty("sw_line_active", True)
                    le.setProperty("inactive_field", None)
                else:
                    le.setProperty("sw_line_active", None)
                    le.setProperty("inactive_field", True)
                le.style().unpolish(le)
                le.style().polish(le)
            # 刷新祖先属性后代的样式（含单选，以应用未选中灰显）
            row.style().unpolish(row)
            row.style().polish(row)
            radio.style().unpolish(radio)
            radio.style().polish(radio)
            for w in (name_lbl, _min_lbl, min_edit, _max_lbl, max_edit):
                w.style().unpolish(w)
                w.style().polish(w)

    def get_special_wear_selection(self) -> tuple[SkinTemplate, float, float] | None:
        if self._mode != "special_wear":
            return None
        for tpl, radio, _nl, _ml, min_e, _xl, max_e, _rw in self._special_rows:
            if radio.isChecked():
                return (tpl, min_e.value(), max_e.value())
        return None

    def find_special_wear_template_by_paint_index(
        self, paint_index: str
    ) -> SkinTemplate | None:
        if self._mode != "special_wear":
            return None
        want = str(paint_index).strip()
        if not want:
            return None
        for tpl, _r, _nl, _ml, _min_e, _xl, _max_e, _rw in self._special_rows:
            if str(tpl.paint_index) == want:
                return tpl
        return None

    def fill_special_wear_rows_from_normalized_range(
        self,
        n_lo: float,
        n_hi: float,
        selected_paint_index: str,
        target_min_wear_raw: str,
        target_max_wear_raw: str,
    ) -> None:
        """与 ``fill_special_wear_rows_from_normalized`` 类似，但选中行为归一化区间 [n_lo, n_hi] 对应的真实 min/max。"""
        if self._mode != "special_wear":
            return
        want = str(selected_paint_index).strip()
        raw_lo = (target_min_wear_raw or "").strip().replace("。", ".")
        raw_hi = (target_max_wear_raw or "").strip().replace("。", ".")
        groups = self._special_wear_all_groups()
        all_edits: list[WearFloatLineEditWithIeee] = []
        for grp in groups:
            for _tpl, _r, _nl, _ml, min_e, _xl, max_e, _rw in grp._special_rows:
                all_edits.extend((min_e, max_e))
        for e in all_edits:
            e.blockSignals(True)
        try:
            for grp in groups:
                for tpl, radio, _nl, _ml, min_e, _xl, max_e, _rw in grp._special_rows:
                    if str(tpl.paint_index) == want:
                        radio.setChecked(True)
                        if raw_lo:
                            min_e.apply_display_text(raw_lo)
                        if raw_hi:
                            max_e.apply_display_text(raw_hi)
                    else:
                        a_lo = SkinTemplate.normalized_to_float(
                            n_lo, tpl.min_float, tpl.max_float
                        )
                        a_hi = SkinTemplate.normalized_to_float(
                            n_hi, tpl.min_float, tpl.max_float
                        )
                        min_e.apply_display_text(format_float_shortest(a_lo))
                        max_e.apply_display_text(format_float_shortest(a_hi))
        finally:
            for e in all_edits:
                e.blockSignals(False)

    def fill_special_wear_rows_from_normalized(
        self,
        nfv: float,
        selected_paint_index: str,
        target_wear_raw: str,
    ) -> None:
        """将同一归一化磨损映射到本组每个产物的最小/最大磨损框；选中 selected 所在行。

        选中行 min=max=target_wear_raw（保留写法）；其余行为该区间的等效真实磨损（点区间）。
        """
        if self._mode != "special_wear":
            return
        want = str(selected_paint_index).strip()
        raw = (target_wear_raw or "").strip().replace("。", ".")
        groups = self._special_wear_all_groups()
        all_edits: list[WearFloatLineEditWithIeee] = []
        for grp in groups:
            for _tpl, _r, _nl, _ml, min_e, _xl, max_e, _rw in grp._special_rows:
                all_edits.extend((min_e, max_e))
        for e in all_edits:
            e.blockSignals(True)
        try:
            for grp in groups:
                for tpl, radio, _nl, _ml, min_e, _xl, max_e, _rw in grp._special_rows:
                    if str(tpl.paint_index) == want:
                        radio.setChecked(True)
                        if raw:
                            min_e.apply_display_text(raw)
                            max_e.apply_display_text(raw)
                    else:
                        actual = SkinTemplate.normalized_to_float(
                            nfv, tpl.min_float, tpl.max_float
                        )
                        s = format_float_shortest(actual)
                        min_e.apply_display_text(s)
                        max_e.apply_display_text(s)
        finally:
            for e in all_edits:
                e.blockSignals(False)

    def try_select_special_wear_row(
        self, paint_index: str, wear_text: str | None
    ) -> bool:
        """选中指定 paint_index 的产物行，可选填入目标磨损；并为本组内其余产物填入等效磨损。"""
        if self._mode != "special_wear":
            return False
        tpl = self.find_special_wear_template_by_paint_index(paint_index)
        if tpl is None:
            return False
        wt = (wear_text or "").strip()
        if not wt:
            for t2, radio, _nl, _ml, _min_e, _xl, _max_e, _rw in self._special_rows:
                if str(t2.paint_index) == str(tpl.paint_index):
                    radio.setChecked(True)
                    return True
            return False
        try:
            w_float = float(wt.replace("。", "."))
        except ValueError:
            return False
        if not (tpl.min_float <= w_float <= tpl.max_float):
            return False
        nfv = SkinTemplate.float_to_normalized(w_float, tpl.min_float, tpl.max_float)
        self.fill_special_wear_rows_from_normalized(
            nfv, str(paint_index).strip(), wt.replace("。", ".")
        )
        return True

    def apply_special_wear_text_to_checked_row(self, wear_text: str) -> bool:
        """仅将磨损填入当前已选中行的最小/最大框（点区间，无 paint_index 预填时使用）。"""
        if self._mode != "special_wear":
            return False
        wt = (wear_text or "").strip()
        if not wt:
            return False
        for _tpl, radio, _nl, _ml, min_e, _xl, max_e, _rw in self._special_rows:
            if radio.isChecked():
                ok_m = min_e.apply_display_text(wt)
                ok_x = max_e.apply_display_text(wt)
                return ok_m and ok_x
        return False

    def collect_by_pid(self) -> dict[str, list]:
        out: dict[str, list] = defaultdict(list)
        if self._mode == "scan":
            for tpl, mn, mx in self._rows:
                out[str(tpl.paint_index)].append((tpl, mn, mx))
        elif self._mode == "target":
            for tpl, te in self._rows:
                out[str(tpl.paint_index)].append((tpl, te))
        else:
            for tpl, _r, _nl, _ml, min_e, _xl, max_e, _rw in self._special_rows:
                out[str(tpl.paint_index)].append((tpl, min_e, max_e))
        return dict(out)

    def _on_header_clicked(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle()

    def toggle(self):
        self._expanded = not self._expanded
        self._update_disclosure_arrow()
        self.content_frame.setVisible(self._expanded)
        self.updateGeometry()
