"""Step-2 mode and special-wear flows extracted from the alchemy page."""

from __future__ import annotations

import math
import time
from collections import defaultdict

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QDialog, QSizePolicy

from config import ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS
from core.alchemy_calc import (
    backfill_missing_substrate_prices,
    eligible_selected_data_for_target,
    partition_selected_data_by_tradeup_group,
)
from core.alchemy_quality import get_template_from_goods_name, get_pid_map
from core.alchemy_special_wear import (
    estimate_special_wear_eta_interval_seconds,
    estimate_special_wear_selection_upper_bound,
)
from core.data_utils import SkinTemplate
from core.float32_wear_prefix import find_float32_range_intersection
from ui.dialogs.alert_dialog import (
    SpecialWearComplexityWarningDialog,
    WearInputNoticeDialog,
)
from ui.feedback import show_alert
from ui.widgets.calc_setting_product_group import CalcSettingProductGroup
from ui.widgets.float_line_edit import format_float_shortest
from ui.widgets.toast import show_toast
from ui.workers.alchemy_workers import CalcProcessRunner, SpecialWearCalcRunner

def _parse_special_target_wear_numeric(raw: str) -> tuple[bool, float, str]:
    s = (raw or "").strip().replace("。", ".")
    if not s:
        return False, 0.0, "请输入目标磨损"
    if s.count(".") > 1:
        return False, 0.0, "请输入有效的数字"
    try:
        v = float(s)
    except ValueError:
        return False, 0.0, "请输入有效的数字"
    if not math.isfinite(v):
        return False, 0.0, "请输入有效的数字"
    return True, v, ""


def _special_target_wear_range_error_message(lo: float, hi: float) -> str:
    return f"目标磨损须在 {format_float_shortest(lo)}～{format_float_shortest(hi)} 之间"


class AlchemyModeMixin:
    def _eligible_selected_data(self) -> list[dict]:
        """计算前按品质/暗金拆组，并静默排除数量不足的组。"""
        return [
            row
            for _quality, _stat_trak, _k, rows
            in partition_selected_data_by_tradeup_group(
                self._selected_data,
                eligible_only=True,
            )
            for row in rows
        ]

    def _step2_refresh_special_wear_norm_cache_from_ui(self) -> None:
        """按当前特殊磨损选中行更新缓存，与扫描/目标模式数值隔离。"""
        if self.get_step2_mode() != "special_wear":
            return
        sw = self._collect_special_wear_target()
        if not sw:
            return
        tpl = self._selected_special_wear_template()
        if tpl is None:
            return
        _pid, w_lo, w_hi = sw
        self._step2_wear_special_min = SkinTemplate.float_to_normalized(
            float(w_lo), tpl.min_float, tpl.max_float
        )
        self._step2_wear_special_max = SkinTemplate.float_to_normalized(
            float(w_hi), tpl.min_float, tpl.max_float
        )

    def get_step2_mode(self) -> str:
        """返回 'scan'、'target' 或 'special_wear'"""
        if self.step2_mode_special_btn.isChecked():
            return "special_wear"
        return "scan" if self.step2_mode_scan_btn.isChecked() else "target"

    def _step3_update_calc_filter_visibility(self) -> None:
        """扫描/目标模式显示最低保本率；特殊磨损模式隐藏。"""
        row = getattr(self, "_step3_min_be_row", None)
        if row is None:
            return
        show_min_be = self.get_step2_mode() in ("scan", "target")
        row.setVisible(show_min_be)

    def _step2_update_norm_row_visibility(self):
        mode = self.get_step2_mode()
        scan = mode == "scan"
        special = mode == "special_wear"
        self.step2_norm_min_label.setVisible(scan)
        self.step2_norm_min_edit.setVisible(scan)
        self.step2_norm_max_label.setVisible(scan)
        self.step2_norm_max_edit.setVisible(scan)
        self.step2_norm_target_label.setVisible(mode == "target")
        self.step2_norm_target_edit.setVisible(mode == "target")
        self.step2_norm_card.setVisible(not special)
        self.step2_special_card.setVisible(special)
        self.step2_scroll.setVisible(True)
        self._step3_update_calc_filter_visibility()

    def _on_step2_mode_changed(self):
        self.step2_mode_container.sync_mode_slider(animate=True)
        self._step2_update_norm_row_visibility()
        if self.step_stack.currentIndex() == 1:
            self._display_step2()

    def _collect_step2_templates_by_box(self) -> dict[str, list]:
        """按武器箱聚合产物模板。"""
        pid_map = get_pid_map()
        product_templates: dict = {}
        skin_names = {
            d.get("goods_name", "")
            for d in self._eligible_selected_data()
            if d.get("goods_name")
        }
        for goods_name in skin_names:
            template = get_template_from_goods_name(goods_name)
            if not template or not template.upper_skins:
                continue
            for pid in template.upper_skins:
                key = str(pid)
                if key not in product_templates:
                    upper = pid_map.get(key)
                    if upper:
                        product_templates[key] = upper

        box_to_templates: dict[str, list] = defaultdict(list)
        for tpl in product_templates.values():
            for box_name in (tpl.weapon_box_name or []):
                box_to_templates[box_name].append(tpl)
        return dict(box_to_templates)

    def _display_step2(self):
        """构建产物磨损列表（第二页）。"""
        self.step_stack.setUpdatesEnabled(False)
        try:
            if self._step2_special_radio_conn is not None:
                QObject.disconnect(self._step2_special_radio_conn)
                self._step2_special_radio_conn = None

            for g in self._step2_box_groups:
                self.step2_groups_layout.removeWidget(g)
                g.deleteLater()
            self._step2_box_groups.clear()
            if self.step2_groups_layout.count() > 0:
                item = self.step2_groups_layout.takeAt(self.step2_groups_layout.count() - 1)
                if item and item.spacerItem():
                    del item

            mode = self.get_step2_mode()
            if mode == "special_wear":
                for c in self._step2_norm_signal_conns:
                    QObject.disconnect(c)
                self._step2_norm_signal_conns = []
                self._step2_product_widgets = {}

            box_to_templates = self._collect_step2_templates_by_box()
            if not box_to_templates:
                return

            for c in self._step2_norm_signal_conns:
                QObject.disconnect(c)
            if mode == "scan":
                self.step2_norm_min_edit.setValue(self._step2_wear_scan_min)
                self.step2_norm_max_edit.setValue(self._step2_wear_scan_max)
                self._step2_norm_signal_conns = [
                    self.step2_norm_min_edit.valueChanged.connect(self._on_step2_norm_changed),
                    self.step2_norm_max_edit.valueChanged.connect(self._on_step2_norm_changed),
                    self.step2_norm_min_edit.errorMessageChanged.connect(self._on_step2_float_error),
                    self.step2_norm_max_edit.errorMessageChanged.connect(self._on_step2_float_error),
                ]
            elif mode == "target":
                self.step2_norm_target_edit.setValue(self._step2_wear_target_norm)
                self._step2_norm_signal_conns = [
                    self.step2_norm_target_edit.valueChanged.connect(self._on_step2_norm_target_changed),
                    self.step2_norm_target_edit.errorMessageChanged.connect(self._on_step2_float_error),
                ]
            else:
                self._step2_norm_signal_conns = []

            parent = self.step2_groups_container
            box_keys = sorted(box_to_templates.keys())
            for bi, box_name in enumerate(box_keys):
                templates = box_to_templates[box_name]
                if mode == "scan":
                    group = CalcSettingProductGroup(
                        box_name,
                        templates,
                        mode,
                        parent,
                        norm_min=self._step2_wear_scan_min,
                        norm_max=self._step2_wear_scan_max,
                    )
                elif mode == "target":
                    group = CalcSettingProductGroup(
                        box_name,
                        templates,
                        mode,
                        parent,
                        norm_target=self._step2_wear_target_norm,
                    )
                else:
                    group = CalcSettingProductGroup(
                        box_name,
                        templates,
                        "special_wear",
                        parent,
                        norm_min=self._step2_wear_special_min,
                        norm_max=self._step2_wear_special_max,
                        special_radio_group=self._step2_special_radio_group,
                        special_is_first_box=(bi == 0),
                    )
                self.step2_groups_layout.addWidget(group)
                self._step2_box_groups.append(group)

            if mode == "special_wear":
                for g in self._step2_box_groups:
                    g.set_special_wear_peer_groups(self._step2_box_groups)

            merged: dict[str, list] = defaultdict(list)
            for group in self._step2_box_groups:
                for pid, entries in group.collect_by_pid().items():
                    merged[pid].extend(entries)
            self._step2_product_widgets = dict(merged)

            self._connect_step2_product_signals(mode)

            self.step2_groups_layout.addStretch(1)

            if mode == "scan":
                self._sync_norm_to_products(self._step2_wear_scan_min, self._step2_wear_scan_max)
            elif mode == "target":
                self._sync_norm_target_to_products(self._step2_wear_target_norm)
            else:
                self._step2_special_radio_conn = self._step2_special_radio_group.buttonToggled.connect(
                    self._on_step2_special_radio_toggled
                )
                for g in self._step2_box_groups:
                    g.update_special_wear_row_active_state()
        finally:
            self.step_stack.setUpdatesEnabled(True)

    def _connect_step2_product_signals(self, mode: str):
        def make_scan_cb(p: str):
            def _go():
                self._on_step2_wear_range_changed(p)
            return _go

        def make_target_cb(p: str):
            def _go():
                self._on_step2_target_changed(p)
            return _go

        err = self._on_step2_float_error
        for pid, widgets_list in self._step2_product_widgets.items():
            if mode == "scan":
                cb = make_scan_cb(pid)
                for _tpl, min_e, max_e in widgets_list:
                    min_e.valueChanged.connect(cb)
                    max_e.valueChanged.connect(cb)
                    min_e.errorMessageChanged.connect(err)
                    max_e.errorMessageChanged.connect(err)
            elif mode == "target":
                cb = make_target_cb(pid)
                for _tpl, target_e in widgets_list:
                    target_e.valueChanged.connect(cb)
                    target_e.errorMessageChanged.connect(err)
            else:
                # special_wear：min/max 仅展示自动推导结果，不允许手动改写。
                continue

    def _on_step2_special_radio_toggled(self, _btn, checked: bool) -> None:
        """含程序化 setChecked（点击行内非按钮区域）时也会触发，buttonClicked 不会。"""
        if not checked:
            return
        for g in self._step2_box_groups:
            g.update_special_wear_row_active_state()
        self._step2_refresh_special_wear_norm_cache_from_ui()
        if (
            self.get_step2_mode() == "special_wear"
            and self.step2_special_target_edit.line_edit().text().strip()
        ):
            self._request_step2_special_target_apply()

    def _collect_special_wear_target(self) -> tuple[str, float, float] | None:
        for g in self._step2_box_groups:
            sel = g.get_special_wear_selection()
            if sel:
                tpl, w_lo, w_hi = sel
                return str(tpl.paint_index), float(w_lo), float(w_hi)
        return None

    def _selected_special_wear_template(self) -> SkinTemplate | None:
        for g in self._step2_box_groups:
            sel = g.get_special_wear_selection()
            if sel:
                tpl, _w_lo, _w_hi = sel
                return tpl
        return None

    def _show_step2_input_error_dialog(self, message: str) -> None:
        """步骤二输入错误：特殊磨损保留弹窗，其余模式改为 Toast。"""
        if self.get_step2_mode() != "special_wear":
            show_toast(self, message, style="warning")
            return
        # 勿在 editingFinished 等焦点栈内同步 exec 模态框，否则易焦点回环/重入卡死
        msg = str(message or "")

        def _deferred_show() -> None:
            show_alert(self, "输入错误", msg)

        QTimer.singleShot(0, _deferred_show)

    def _on_step2_special_target_text_edited(self, _text: str) -> None:
        self._step2_special_target_dirty = True

    def _request_step2_special_target_apply(self) -> None:
        if not self._step2_special_target_apply_timer.isActive():
            self._step2_special_target_apply_timer.start(50)

    def _run_step2_special_target_apply(self) -> None:
        self._apply_step2_special_target_wear(show_dialog=True)

    def _apply_step2_special_target_wear(self, *, show_dialog: bool) -> bool:
        if self.get_step2_mode() != "special_wear":
            return False
        tpl = self._selected_special_wear_template()
        if tpl is None:
            if show_dialog:
                self._show_step2_input_error_dialog("请先选择目标产物")
            return False
        raw = self.step2_special_target_edit.line_edit().text().strip().replace("。", ".")
        ok, target_wear, err = _parse_special_target_wear_numeric(raw)
        if not ok:
            if show_dialog:
                self._show_step2_input_error_dialog(err)
            return False
        lo = float(tpl.min_float)
        hi = float(tpl.max_float)
        if target_wear < lo or target_wear > hi:
            if show_dialog:
                self._show_step2_input_error_dialog(
                    _special_target_wear_range_error_message(lo, hi)
                )
            return False
        f_lo, f_hi, f32_err = find_float32_range_intersection(raw, lo, hi)
        if f32_err or f_lo is None or f_hi is None:
            if show_dialog:
                self._show_step2_input_error_dialog(
                    f32_err or "无法推导目标磨损区间"
                )
            return False
        n_lo = SkinTemplate.float_to_normalized(f_lo, tpl.min_float, tpl.max_float)
        n_hi = SkinTemplate.float_to_normalized(f_hi, tpl.min_float, tpl.max_float)
        self._step2_wear_special_min = n_lo
        self._step2_wear_special_max = n_hi
        if self._step2_box_groups:
            self._step2_box_groups[0].fill_special_wear_rows_from_normalized_range(
                n_lo,
                n_hi,
                str(tpl.paint_index).strip(),
                format_float_shortest(f_lo),
                format_float_shortest(f_hi),
            )
        for g in self._step2_box_groups:
            g.update_special_wear_row_active_state()
        return True

    def _on_step2_special_target_editing_finished(self) -> None:
        if not self._step2_special_target_dirty:
            return
        self._step2_special_target_dirty = False
        self._request_step2_special_target_apply()

    def _on_step2_wear_range_changed(self, source_pid: str):
        """当某产物的最小/最大磨损度变化时，计算 normalized 并同步到所有产物"""
        if source_pid not in self._step2_product_widgets:
            return
        widgets_list = self._step2_product_widgets[source_pid]
        sender = self.sender()
        tpl, min_spin, max_spin = widgets_list[0]
        for t, mn, mx in widgets_list:
            if sender is mn or sender is mx:
                tpl, min_spin, max_spin = t, mn, mx
                break
        user_min = min_spin.value()
        user_max = max_spin.value()
        span = tpl.max_float - tpl.min_float
        if span <= 0:
            return
        if user_min > user_max:
            self._show_step2_input_error_dialog("最小磨损度不能超过最大磨损度")
            prev_min = SkinTemplate.normalized_to_float(
                self._step2_wear_scan_min, tpl.min_float, tpl.max_float
            )
            prev_max = SkinTemplate.normalized_to_float(
                self._step2_wear_scan_max, tpl.min_float, tpl.max_float
            )
            if sender is min_spin:
                min_spin.blockSignals(True)
                min_spin.setValue(prev_min)
                min_spin.blockSignals(False)
            else:
                max_spin.blockSignals(True)
                max_spin.setValue(prev_max)
                max_spin.blockSignals(False)
            return
        norm_min = SkinTemplate.float_to_normalized(user_min, tpl.min_float, tpl.max_float)
        norm_max = SkinTemplate.float_to_normalized(user_max, tpl.min_float, tpl.max_float)

        self._step2_wear_scan_min = norm_min
        self._step2_wear_scan_max = norm_max

        for pid, other_widgets in self._step2_product_widgets.items():
            for other_tpl, other_min, other_max in other_widgets:
                if pid == source_pid and (other_min, other_max) == (min_spin, max_spin):
                    continue
                other_min.blockSignals(True)
                other_max.blockSignals(True)
                other_min.setValue(SkinTemplate.normalized_to_float(norm_min, other_tpl.min_float, other_tpl.max_float))
                other_max.setValue(SkinTemplate.normalized_to_float(norm_max, other_tpl.min_float, other_tpl.max_float))
                other_min.blockSignals(False)
                other_max.blockSignals(False)

        self.step2_norm_min_edit.blockSignals(True)
        self.step2_norm_max_edit.blockSignals(True)
        self.step2_norm_min_edit.setValue(norm_min)
        self.step2_norm_max_edit.setValue(norm_max)
        self.step2_norm_min_edit.blockSignals(False)
        self.step2_norm_max_edit.blockSignals(False)

    def _on_step2_float_error(self, msg: str):
        """任一磨损输入框校验失败时提醒。"""
        if msg:
            self._show_step2_input_error_dialog(msg)

    def _sync_norm_to_products(self, norm_min: float, norm_max: float):
        """将归一化范围同步到所有产物（扫描模式）"""
        self._step2_wear_scan_min = norm_min
        self._step2_wear_scan_max = norm_max
        for pid, widgets_list in self._step2_product_widgets.items():
            tpl = widgets_list[0][0]
            for _, min_edit, max_edit in widgets_list:
                min_edit.blockSignals(True)
                max_edit.blockSignals(True)
                min_edit.setValue(SkinTemplate.normalized_to_float(norm_min, tpl.min_float, tpl.max_float))
                max_edit.setValue(SkinTemplate.normalized_to_float(norm_max, tpl.min_float, tpl.max_float))
                min_edit.blockSignals(False)
                max_edit.blockSignals(False)

    def _on_step2_norm_changed(self):
        """当顶部归一化磨损度（扫描模式）变化时，同步到所有产物"""
        norm_min = self.step2_norm_min_edit.value()
        norm_max = self.step2_norm_max_edit.value()
        norm_min = max(0.0, min(1.0, norm_min))
        norm_max = max(0.0, min(1.0, norm_max))
        if norm_min > norm_max:
            self._show_step2_input_error_dialog("最小磨损度不能超过最大磨损度")
            self.step2_norm_min_edit.blockSignals(True)
            self.step2_norm_max_edit.blockSignals(True)
            self.step2_norm_min_edit.setValue(self._step2_wear_scan_min)
            self.step2_norm_max_edit.setValue(self._step2_wear_scan_max)
            self.step2_norm_min_edit.blockSignals(False)
            self.step2_norm_max_edit.blockSignals(False)
            return

        self._sync_norm_to_products(norm_min, norm_max)

    def _on_step2_norm_target_changed(self):
        """当顶部归一化目标磨损度变化时，同步到所有产物"""
        norm_target = self.step2_norm_target_edit.value()
        norm_target = max(0.0, min(1.0, norm_target))
        self._step2_wear_target_norm = norm_target
        self._sync_norm_target_to_products(norm_target)

    def _on_step2_target_changed(self, source_pid: str):
        """当某产物的目标磨损度变化时，计算归一化并同步到所有产物"""
        if source_pid not in self._step2_product_widgets:
            return
        widgets_list = self._step2_product_widgets[source_pid]
        sender = self.sender()
        tpl, target_edit = widgets_list[0]
        for t, te in widgets_list:
            if sender is te:
                tpl, target_edit = t, te
                break
        user_val = target_edit.value()
        norm_target = SkinTemplate.float_to_normalized(user_val, tpl.min_float, tpl.max_float)
        norm_target = max(0.0, min(1.0, norm_target))
        self._step2_wear_target_norm = norm_target

        for pid, other_widgets in self._step2_product_widgets.items():
            for other_tpl, other_target in other_widgets:
                if pid == source_pid and other_target is target_edit:
                    continue
                other_target.blockSignals(True)
                other_target.setValue(SkinTemplate.normalized_to_float(
                    norm_target, other_tpl.min_float, other_tpl.max_float
                ))
                other_target.blockSignals(False)

        self.step2_norm_target_edit.blockSignals(True)
        self.step2_norm_target_edit.setValue(norm_target)
        self.step2_norm_target_edit.blockSignals(False)

    def _sync_norm_target_to_products(self, norm_target: float):
        """将归一化目标磨损度同步到所有产物（目标模式）"""
        self._step2_wear_target_norm = norm_target
        for pid, widgets_list in self._step2_product_widgets.items():
            tpl = widgets_list[0][0]
            for _, target_edit in widgets_list:
                target_edit.blockSignals(True)
                target_edit.setValue(SkinTemplate.normalized_to_float(
                    norm_target, tpl.min_float, tpl.max_float
                ))
                target_edit.blockSignals(False)

    def _on_step2_reset_wear(self):
        """重置磨损度：scan 模式重置到 min/max 范围，target 模式重置归一化目标到 1.0"""
        if self.get_step2_mode() == "special_wear":
            self.step2_special_target_edit.line_edit().clear()
            self._step2_special_target_dirty = False
            self._step2_special_target_apply_timer.stop()
            self.step2_special_rounds_spin.setValue(ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS)
            for _pid, widgets_list in self._step2_product_widgets.items():
                tpl = widgets_list[0][0]
                mn, mx = tpl.min_float, tpl.max_float
                for _, min_spin, max_spin in widgets_list:
                    min_spin.blockSignals(True)
                    max_spin.blockSignals(True)
                    min_spin.setValue(mn)
                    max_spin.setValue(mx)
                    min_spin.blockSignals(False)
                    max_spin.blockSignals(False)
            self._step2_wear_special_min = 0.0
            self._step2_wear_special_max = 1.0
            self._display_step2()
            return
        if self.get_step2_mode() == "scan":
            for pid, widgets_list in self._step2_product_widgets.items():
                tpl = widgets_list[0][0]
                mn, mx = tpl.min_float, tpl.max_float
                for _, min_spin, max_spin in widgets_list:
                    min_spin.blockSignals(True)
                    max_spin.blockSignals(True)
                    min_spin.setValue(mn)
                    max_spin.setValue(mx)
                    min_spin.blockSignals(False)
                    max_spin.blockSignals(False)
            self._step2_wear_scan_min = 0.0
            self._step2_wear_scan_max = 1.0
            self.step2_norm_min_edit.blockSignals(True)
            self.step2_norm_max_edit.blockSignals(True)
            self.step2_norm_min_edit.setValue(0.0)
            self.step2_norm_max_edit.setValue(1.0)
            self.step2_norm_min_edit.blockSignals(False)
            self.step2_norm_max_edit.blockSignals(False)
        else:
            self._step2_wear_target_norm = 1.0
            self.step2_norm_target_edit.blockSignals(True)
            self.step2_norm_target_edit.setValue(1.0)
            self.step2_norm_target_edit.blockSignals(False)
            self._sync_norm_target_to_products(1.0)

    def _on_step2_wear_notice_clicked(self) -> None:
        dlg = WearInputNoticeDialog(self.window())
        dlg.exec()

    def _on_step2_prev(self):
        """磨损设置 - 上一步，返回数据选择页"""
        self.step_stack.setCurrentIndex(0)

    def _on_step2_next(self):
        """磨损设置 - 下一步，进入计算设置"""
        if self.get_step2_mode() == "special_wear":
            sw = self._collect_special_wear_target()
            if sw:
                _pid, w_lo, w_hi = sw
                if (w_hi - w_lo) > 0.01:
                    dlg = SpecialWearComplexityWarningDialog(self.window())
                    if dlg.exec() != QDialog.Accepted:
                        return
        self._step3_update_calc_filter_visibility()
        self.step_stack.setCurrentIndex(2)


    def _on_fetch_price_finished(self, price_map, error_msg: str | None):
        """产物价格加载完成 - 成功则启动计算进程，失败则弹窗"""
        if error_msg == "__cancelled__":
            self._step3_reset_after_calc_interrupted()
            return
        if error_msg:
            self._step3_set_calc_button_idle()
            show_alert(self, "加载产物价格失败", error_msg)
            return

        self._selected_data, _repriced, _unresolved = backfill_missing_substrate_prices(
            self._selected_data,
            price_map,
        )

        mode = self.get_step2_mode()
        if mode == "special_wear":
            sw = self._collect_special_wear_target()
            if not sw:
                self._step3_set_calc_button_idle()
                self._show_step3_input_error("请选择目标产物")
                return
            target_pid, target_wear_lo, target_wear_hi = sw
            if target_wear_lo > target_wear_hi:
                self._step3_set_calc_button_idle()
                self._show_step3_input_error("最小磨损度不能大于最大磨损度")
                return
            self._disconnect_calc_runner_signals()
            self._calc_start_time = time.time()
            sw_rounds = int(self.step2_special_rounds_spin.value())
            calculation_data = eligible_selected_data_for_target(
                self._selected_data,
                target_pid,
            )
            n_ub, k_ub, tc_ub = estimate_special_wear_selection_upper_bound(
                calculation_data
            )
            _, self._sw_eta_high = estimate_special_wear_eta_interval_seconds(
                n_ub, k_ub, tc_ub, sw_rounds
            )
            self._special_wear_runner = SpecialWearCalcRunner(
                calculation_data,
                price_map,
                target_pid,
                target_wear_lo,
                target_wear_hi,
                None,
                rounds=sw_rounds,
                non_overlapping_recipes=self.step3_no_overlap_check.isChecked(),
                parent=self,
            )
            self._special_wear_finished_conn = self._special_wear_runner.finished.connect(
                self._on_calc_finished
            )
            self.step3_progress_bar.setVisible(True)
            self.step3_progress_bar.setValue(0)
            self.step3_progress_label.setFixedWidth(44)
            self.step3_progress_label.setMinimumWidth(0)
            self.step3_progress_label.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred,
            )
            self.step3_progress_label.setText("0%")
            self.step3_progress_detail_label.setVisible(True)
            self.step3_progress_container.setVisible(True)
            self._special_wear_progress_conn = self._special_wear_runner.progress.connect(
                self._on_special_wear_progress
            )
            self._special_wear_stats_conn = self._special_wear_runner.progress_stats.connect(
                self._on_special_wear_progress_stats
            )
            self._special_wear_runner.start()
            self._start_special_wear_elapsed_timer()
            return

        if mode not in ("scan", "target"):
            self._step3_set_calc_button_idle()
            self._show_step3_input_error(
                f"无效的 mode: {mode}，仅支持 scan 或 target"
            )
            return
        if mode == "scan":
            norm_min = self.step2_norm_min_edit.value()
            norm_max = self.step2_norm_max_edit.value()
        else:
            norm_min = self.step2_norm_target_edit.value()
            norm_max = norm_min

        self._disconnect_calc_runner_signals()
        self._calc_start_time = time.time()
        min_break_even_rate = self.step3_min_be_spin.value() / 100.0

        self._calc_worker = CalcProcessRunner(
            self._selected_data,
            price_map,
            norm_min,
            norm_max,
            mode,
            min_break_even_rate=min_break_even_rate,
            non_overlapping_recipes=self.step3_no_overlap_check.isChecked(),
        )
        self._calc_finished_conn = self._calc_worker.finished.connect(self._on_calc_finished)
        self._calc_progress_conn = None
        if mode == "scan":
            self.step3_progress_detail_label.setVisible(False)
            self.step3_progress_bar.setValue(0)
            self.step3_progress_label.setText("0%")
            self.step3_progress_container.setVisible(True)
            self._calc_progress_conn = self._calc_worker.progress.connect(self._on_calc_progress)
        self._calc_worker.start()
        show_toast(self, "计算中，请稍候...", style="info")

    def _on_calc_progress(self, pct: int):
        """扫描模式下更新进度条"""
        # 搜索任务全部返回后仍需做去重与结果整理；真正 finished 前最多显示 99%。
        display_pct = max(0, min(99, int(pct)))
        self.step3_progress_bar.setValue(display_pct)
        self.step3_progress_label.setText(f"{display_pct}%")

    def _stop_special_wear_elapsed_timer(self) -> None:
        self._special_wear_elapsed_timer.stop()
        self._special_wear_timing_active = False

    def _start_special_wear_elapsed_timer(self) -> None:
        self._special_wear_last_pct = 0
        self._special_wear_timing_active = True
        self._on_special_wear_elapsed_tick()
        self._special_wear_elapsed_timer.start()

    def _on_special_wear_elapsed_tick(self) -> None:
        """预计总耗时只展示区间上界（与已用时间取 max），来自 n、k、C(n,k) 与轮数。"""
        if not self._special_wear_timing_active:
            return
        t0 = getattr(self, "_calc_start_time", None)
        if t0 is None:
            return
        elapsed = max(0.0, time.time() - float(t0))
        pct = max(0, min(100, int(self._special_wear_last_pct)))
        eta_hi = max(0.0, float(getattr(self, "_sw_eta_high", 30.0)))

        def _fmt_est_sec(t: float) -> str:
            t = max(0.0, float(t))
            if t > 86400:
                return "超过 24 小时"
            if t > 3600:
                return f"约 {t / 3600:.1f} 小时"
            if t >= 60:
                return f"约 {t / 60:.0f} 分钟"
            return f"约 {t:.0f} 秒"

        if pct >= 100:
            self.step3_progress_detail_label.setText(
                f"全程合计 {elapsed:.1f} 秒 · 收尾中…"
            )
            return

        t_high = max(elapsed, eta_hi)
        self.step3_progress_detail_label.setText(
            f"预计需要 {_fmt_est_sec(t_high)}（已用 {elapsed:.1f} 秒）"
        )

    def _on_special_wear_progress_stats(self, payload: object) -> None:
        """用抽样后的 n、k、C(n,k) 细化总耗时估算（MITM 仍有剪枝，仍为启发式）。"""
        if not isinstance(payload, tuple) or len(payload) != 7:
            return
        checked, total, nodes, n_inst, k, round_idx, total_rounds = payload
        del checked, nodes, round_idx
        if n_inst > 0 and total > 0 and k > 0:
            _, self._sw_eta_high = estimate_special_wear_eta_interval_seconds(
                n_inst, k, total, total_rounds
            )

    def _on_special_wear_progress(self, pct: int) -> None:
        p = max(0, min(100, int(pct)))
        self._special_wear_last_pct = p
        self.step3_progress_bar.setValue(p)
        self.step3_progress_label.setText(f"{p}%")

