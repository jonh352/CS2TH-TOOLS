from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.alchemy_quality import get_template_from_goods_name
from core.data_utils import SkinInstance
from core.platform_links import MARKETPLACES, links_for_template
from core.purchase_batches import (
    apply_purchase_batch_replacement,
    delete_purchase_batch,
    load_purchase_batch,
    purchase_batch_replacement_options,
    purchase_batch_summary,
    reconcile_all_purchase_records_for_profile,
    set_purchase_batch_material_status,
    toggle_all_purchase_batch_materials_ordered,
)
from core.purchase_tracking import (
    STATUS_CANCELLED,
    STATUS_ORDERED,
    STATUS_PENDING,
    STATUS_RECEIVED,
    load_profile_inventory_items,
)
from ui.dialogs.purchase_replacement_dialog import PurchaseReplacementDialog
from ui.feedback import ask_confirmation
from ui.icons import expand_section_triangle_icon
from ui.widgets.toast import show_toast


_STATUS_LABELS = {
    STATUS_PENDING: "待购买",
    STATUS_ORDERED: "待入库",
    STATUS_RECEIVED: "已入库",
    STATUS_CANCELLED: "需补购",
}
_FILTER_TO_STATUS = {
    "全部材料": "",
    "未入库（全部）": "not_received",
    "待购买": STATUS_PENDING,
    "待入库": STATUS_ORDERED,
    "已入库": STATUS_RECEIVED,
    "需补购": STATUS_CANCELLED,
}


def _recipe_group_label(index: int) -> str:
    """Compact chronological recipe label: ①..⑳, then circled-number fallback."""
    if 1 <= index <= 20:
        return chr(0x2460 + index - 1)
    if 21 <= index <= 35:
        return chr(0x3251 + index - 21)
    if 36 <= index <= 50:
        return chr(0x32B1 + index - 36)
    return f"#{index}"


def _valid_price(value: object) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _planned_material_price(material: dict, substrate: dict) -> float | None:
    replacement = material.get("replacement")
    if isinstance(replacement, dict):
        replacement_price = _valid_price(replacement.get("purchase_price"))
        if replacement_price is not None:
            return replacement_price
    return _valid_price(material.get("price")) or _valid_price(substrate.get("price"))


def _planned_recipe_cost(entry: dict) -> float | None:
    recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
    substrates = recipe.get("substrates_display") or []
    prices: list[float] = []
    for material in entry.get("materials") or []:
        if not isinstance(material, dict):
            continue
        try:
            index = int(material.get("substrate_index"))
            substrate = substrates[index]
        except (IndexError, TypeError, ValueError):
            substrate = {}
        price = _planned_material_price(
            material,
            substrate if isinstance(substrate, dict) else {},
        )
        if price is not None:
            prices.append(price)
    if prices and len(prices) == len(entry.get("materials") or []):
        return sum(prices)
    return _valid_price(recipe.get("cost"))


def _planned_batch_cost(payload: dict) -> float:
    return sum(
        float(_planned_recipe_cost(entry) or 0.0)
        for entry in payload.get("recipes") or []
        if isinstance(entry, dict)
    )


class PurchaseBatchCard(QFrame):
    changed = Signal()
    deleted = Signal()
    change_account_requested = Signal(object)

    def __init__(
        self,
        path: Path,
        payload: dict,
        parent: QWidget | None = None,
        *,
        expanded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("recipeManageRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._path = path
        self._payload = payload
        self._expanded = bool(expanded)
        self._table: QTableWidget | None = None
        self._tables: list[QTableWidget] = []
        self._detail_widgets: list[QWidget] = []
        self._recipe_group_buttons: list[QPushButton] = []
        self._expanded_recipe_ids: set[str] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(9)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        self._title_label = QLabel(self)
        self._title_label.setObjectName("recipeSavedTitle")
        self._summary_label = QLabel(self)
        self._summary_label.setObjectName("alchemyStep1Hint")
        title_box.addWidget(self._title_label)
        title_box.addWidget(self._summary_label)
        header.addLayout(title_box, 1)
        controls_grid = QGridLayout()
        controls_grid.setContentsMargins(0, 0, 0, 0)
        controls_grid.setHorizontalSpacing(12)
        controls_grid.setVerticalSpacing(8)
        controls_grid.addLayout(header, 0, 0)
        self._toggle_button = QPushButton(self)
        self._toggle_button.setObjectName("purchaseBatchViewMaterialsBtn")
        self._toggle_button.setMinimumSize(190, 42)
        self._toggle_button.setToolTip("展开或收起该批次的配方和采购明细")
        self._toggle_button.clicked.connect(self._toggle)
        self._reconcile_button = QPushButton("核对库存", self)
        self._reconcile_button.setObjectName("purchaseBatchReconcileBtn")
        self._reconcile_button.setMinimumSize(190, 42)
        self._reconcile_button.setToolTip(
            "使用该 Steam 账号最近一次刷新到本地的库存核对"
        )
        self._reconcile_button.clicked.connect(self._reconcile)
        controls_grid.addWidget(
            self._reconcile_button,
            0,
            1,
            2,
            1,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        controls_grid.addWidget(
            self._toggle_button,
            0,
            2,
            2,
            1,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        controls_grid.setColumnStretch(0, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        filter_label = QLabel("材料筛选", self)
        filter_label.setObjectName("alchemyStep1Hint")
        actions.addWidget(filter_label)
        self._filter = QComboBox(self)
        self._filter.addItems(tuple(_FILTER_TO_STATUS))
        self._filter.setToolTip("按采购和入库状态筛选；选择后会自动展开材料明细")
        self._filter.currentTextChanged.connect(self._on_filter_changed)
        actions.addWidget(self._filter)
        self._mark_all_button = QPushButton(self)
        self._mark_all_button.clicked.connect(self._toggle_all_ordered)
        actions.addWidget(self._mark_all_button)
        change_account = QPushButton("修改账号", self)
        change_account.setToolTip("重新选择该批次的 Steam 收货账号")
        change_account.clicked.connect(
            lambda _checked=False: self.change_account_requested.emit(self._path)
        )
        actions.addWidget(change_account)
        delete = QPushButton("删除批次", self)
        delete.setObjectName("alchemyClearFileBtn")
        delete.clicked.connect(self._delete)
        actions.addWidget(delete)
        actions.addStretch(1)
        controls_grid.addLayout(actions, 1, 0)
        root.addLayout(controls_grid)
        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        workflow_hint = QLabel(
            "采购流程：逐件“打开”购买 → 购买成功后“标记已买” → "
            "刷新对应 Steam 库存后“核对库存”。未到账的材料可标记为“没买到”，"
            "再筛选“需补购”查看原项重购或安全替代建议。",
            self._content,
        )
        workflow_hint.setObjectName("alchemyStep1Hint")
        workflow_hint.setWordWrap(True)
        self._content_layout.addWidget(workflow_hint)
        self._content.setVisible(self._expanded)
        root.addWidget(self._content)
        self._refresh_header()
        if self._expanded:
            self._rebuild_table()

    def _refresh_header(self) -> None:
        summary = purchase_batch_summary(self._payload)
        self._title_label.setText(str(self._payload.get("name") or "未命名采购批次"))
        self._summary_label.setText(
            f"账号：{self._payload.get('account_name') or 'Steam'} · "
            f"{summary['recipes']} 个配方 / {summary['total']} 件 · "
            f"已入库 {summary[STATUS_RECEIVED]} · 待入库 {summary[STATUS_ORDERED]} · "
            f"待购买 {summary[STATUS_PENDING]} · 需补购 {summary[STATUS_CANCELLED]} · "
            f"批次总成本 ¥{_planned_batch_cost(self._payload):.2f}"
        )
        self._toggle_button.setText(
            "收起材料" if self._expanded else f"查看材料（{summary['total']}）"
        )
        can_toggle = (
            summary[STATUS_PENDING]
            + summary[STATUS_ORDERED]
            + summary[STATUS_CANCELLED]
        ) > 0
        undo_all = (
            can_toggle
            and summary[STATUS_PENDING] == 0
            and summary[STATUS_CANCELLED] == 0
            and summary[STATUS_ORDERED] > 0
        )
        self._mark_all_button.setText(
            "全部撤销已买" if undo_all else "全部标记已买"
        )
        self._mark_all_button.setEnabled(can_toggle)

    def _toggle(self) -> None:
        self._set_expanded(not self._expanded)

    def ui_state(self) -> dict[str, object]:
        """Return transient view state so disk refreshes do not reset the card."""
        return {
            "expanded": self._expanded,
            "filter": self._filter.currentText(),
            "expanded_recipe_ids": tuple(self._expanded_recipe_ids),
        }

    def restore_ui_state(self, state: dict[str, object] | None) -> None:
        if not isinstance(state, dict):
            return
        filter_text = str(state.get("filter") or "全部材料")
        if filter_text not in _FILTER_TO_STATUS:
            filter_text = "全部材料"
        self._filter.blockSignals(True)
        self._filter.setCurrentText(filter_text)
        self._filter.blockSignals(False)
        recipe_ids = state.get("expanded_recipe_ids")
        if isinstance(recipe_ids, (list, tuple, set, frozenset)):
            self._expanded_recipe_ids = {
                str(recipe_id) for recipe_id in recipe_ids if str(recipe_id)
            }
        else:
            self._expanded_recipe_ids.clear()
        self._set_expanded(bool(state.get("expanded")))

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        summary = purchase_batch_summary(self._payload)
        self._toggle_button.setText(
            "收起材料" if self._expanded else f"查看材料（{summary['total']}）"
        )
        self._content.setVisible(self._expanded)
        if self._expanded:
            self._rebuild_table()

    def _on_filter_changed(self, _text: str) -> None:
        if not self._expanded:
            self._set_expanded(True)
            return
        self._rebuild_table()

    def _recipe_groups(self) -> list[tuple[str, dict, list[tuple[dict, dict]]]]:
        wanted = _FILTER_TO_STATUS.get(self._filter.currentText(), "")
        groups: list[tuple[str, dict, list[tuple[dict, dict]]]] = []
        entries = [
            entry
            for entry in self._payload.get("recipes") or []
            if isinstance(entry, dict)
        ]
        entries.sort(
            key=lambda entry: str(entry.get("added_at") or "")
        )
        for recipe_index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            group_label = _recipe_group_label(recipe_index)
            recipe = entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
            substrates = recipe.get("substrates_display") or []
            rows: list[tuple[dict, dict]] = []
            for material in entry.get("materials") or []:
                if not isinstance(material, dict):
                    continue
                status = str(material.get("status") or STATUS_PENDING)
                if wanted == "not_received" and status == STATUS_RECEIVED:
                    continue
                if wanted and wanted != "not_received" and status != wanted:
                    continue
                try:
                    substrate = substrates[int(material.get("substrate_index"))]
                except (IndexError, TypeError, ValueError):
                    substrate = {}
                rows.append(
                    (material, substrate if isinstance(substrate, dict) else {})
                )
            if rows:
                groups.append((group_label, entry, rows))
        return groups

    def _rebuild_table(self) -> None:
        if not self._expanded:
            return
        for widget in self._detail_widgets:
            self._content_layout.removeWidget(widget)
            widget.deleteLater()
        self._detail_widgets.clear()
        self._tables.clear()
        self._recipe_group_buttons.clear()
        self._table = None
        groups = self._recipe_groups()
        if self._filter.currentText() != "全部材料":
            self._expanded_recipe_ids.update(
                str(entry.get("id") or "") for _label, entry, _rows in groups
            )
        if not groups:
            empty = QLabel("当前筛选条件下没有材料", self._content)
            empty.setObjectName("alchemyStep1Hint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._content_layout.addWidget(empty)
            self._detail_widgets.append(empty)
            return
        for group_label, entry, rows in groups:
            group = self._build_recipe_group(group_label, entry, rows)
            self._content_layout.addWidget(group)
            self._detail_widgets.append(group)

    def _build_recipe_group(
        self,
        group_label: str,
        entry: dict,
        rows: list[tuple[dict, dict]],
    ) -> QFrame:
        entry_id = str(entry.get("id") or "")
        expanded = entry_id in self._expanded_recipe_ids
        group = QFrame(self._content)
        group.setObjectName("alchemyGroup")
        group.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(group)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame(group)
        header.setObjectName("alchemyGroupHeader")
        header.setFixedHeight(44)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        header_layout.setSpacing(8)
        toggle = QPushButton(group_label, header)
        toggle.setFlat(True)
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setToolTip(str(entry.get("title") or f"配方 {group_label}"))
        toggle.setStyleSheet("text-align: left; border: none; background: transparent;")
        toggle.setIcon(
            expand_section_triangle_icon(
                expanded,
                size_px=14,
                fill_color="#10b981",
            )
        )
        self._recipe_group_buttons.append(toggle)
        header_layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        status_counts = {status: 0 for status in _STATUS_LABELS}
        for material in entry.get("materials") or []:
            if isinstance(material, dict):
                status = str(material.get("status") or STATUS_PENDING)
                if status in status_counts:
                    status_counts[status] += 1
        summary = QLabel(
            f"{len(entry.get('materials') or [])}件 · "
            f"已入库{status_counts[STATUS_RECEIVED]} · "
            f"待入库{status_counts[STATUS_ORDERED]} · "
            f"待购买{status_counts[STATUS_PENDING]} · "
            f"需补购{status_counts[STATUS_CANCELLED]} · "
            f"成本 ¥{float(_planned_recipe_cost(entry) or 0):.2f}",
            header,
        )
        summary.setObjectName("alchemyStep1Hint")
        header_layout.addWidget(summary, 1, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)
        content = QFrame(group)
        content.setObjectName("alchemyTableFrame")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 8, 10, 10)
        table = self._build_material_table(entry, rows, content)
        content_layout.addWidget(table)
        content.setVisible(expanded)
        root.addWidget(content)

        def toggle_recipe(_checked: bool = False) -> None:
            is_expanded = not content.isVisible()
            content.setVisible(is_expanded)
            if is_expanded:
                self._expanded_recipe_ids.add(entry_id)
            else:
                self._expanded_recipe_ids.discard(entry_id)
            toggle.setIcon(
                expand_section_triangle_icon(
                    is_expanded,
                    size_px=14,
                    fill_color="#10b981",
                )
            )

        toggle.clicked.connect(toggle_recipe)
        return group

    def _build_material_table(
        self,
        entry: dict,
        rows: list[tuple[dict, dict]],
        parent: QWidget,
    ) -> QTableWidget:
        table = QTableWidget(len(rows), 8, parent)
        self._tables.append(table)
        if self._table is None:
            self._table = table
        table.setHorizontalHeaderLabels(
            [
                "序号",
                "材料",
                "计划磨损",
                "采集价格",
                "状态",
                "实际入库",
                "购买",
                "操作",
            ]
        )
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setMinimumSectionSize(48)
        table.verticalHeader().setDefaultSectionSize(48)
        table.setMinimumHeight(min(620, 56 + max(1, len(rows)) * 48))
        table.setMaximumHeight(620)
        widths = (48, 250, 180, 90, 90, 120, 96, 200)
        for column, width in enumerate(widths):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(column, width)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        align = Qt.AlignmentFlag.AlignCenter
        for index, (material, substrate) in enumerate(rows, start=1):
            status = str(material.get("status") or STATUS_PENDING)
            replacement = material.get("replacement")
            planned_name = (
                str(replacement.get("name") or "")
                if isinstance(replacement, dict)
                else str(substrate.get("name") or material.get("name") or "")
            )
            if isinstance(replacement, dict):
                if replacement.get("manual_wear") is not None:
                    decimals = int(replacement.get("manual_wear_decimals") or 6)
                    planned_wear = (
                        f"{float(replacement.get('manual_wear')):.{decimals}f}..."
                    )
                else:
                    planned_wear = (
                        f"{float(replacement.get('min_wear')):.10f} ～ "
                        f"{float(replacement.get('max_wear')):.10f}"
                    )
            else:
                planned_wear = f"{float(material.get('float_value') or 0):.18f}"
            actual = "-"
            if status == STATUS_RECEIVED:
                actual = f"{float(material.get('matched_float') or 0):.18f}"
            planned_price = _planned_material_price(material, substrate)
            values = (
                str(index),
                planned_name,
                planned_wear,
                f"¥{planned_price:.2f}" if planned_price is not None else "-",
                _STATUS_LABELS.get(status, status),
                actual,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(align)
                if column == 4:
                    item.setForeground(
                        QColor(
                            "#10b981"
                            if status == STATUS_RECEIVED
                            else "#f59e0b"
                            if status == STATUS_ORDERED
                            else "#ef4444"
                            if status == STATUS_CANCELLED
                            else "#64748b"
                        )
                    )
                table.setItem(index - 1, column, item)
            table.setCellWidget(index - 1, 6, self._purchase_cell(material, substrate))
            table.setCellWidget(index - 1, 7, self._action_cell(entry, material))
        return table

    def _purchase_cell(self, material: dict, substrate: dict) -> QWidget:
        wrap = QWidget(self)
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(4, 2, 4, 2)
        button = QPushButton("打开", wrap)
        button.setMinimumHeight(34)
        replacement = material.get("replacement")
        if isinstance(replacement, dict):
            template = get_template_from_goods_name(str(replacement.get("name") or ""))
            if template is None:
                button.setEnabled(False)
            else:
                low = float(replacement.get("min_wear"))
                high = float(replacement.get("max_wear"))
                appearance = SkinInstance.get_appearance((low + high) / 2.0) or ""
                links = links_for_template(template, appearance, min_wear=low, max_wear=high)
                menu = QMenu(button)
                for market in MARKETPLACES:
                    url = str(links.get(market.key) or "")
                    if not url or url == market.home_url:
                        continue
                    action = menu.addAction(market.name)
                    action.triggered.connect(
                        lambda _checked=False, value=url: QDesktopServices.openUrl(QUrl(value))
                    )
                button.setMenu(menu)
                button.setEnabled(not menu.isEmpty())
        else:
            url = str(substrate.get("purchase_link") or "")
            button.setEnabled(bool(url))
            button.clicked.connect(
                lambda _checked=False, value=url: QDesktopServices.openUrl(QUrl(value))
            )
        layout.addWidget(button)
        return wrap

    def _action_cell(self, entry: dict, material: dict) -> QWidget:
        wrap = QWidget(self)
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(3, 2, 3, 2)
        layout.setSpacing(4)
        status = str(material.get("status") or STATUS_PENDING)
        entry_id = str(entry.get("id") or "")
        row_id = str(material.get("row_id") or "")
        if status == STATUS_PENDING:
            self._add_action_button(layout, "标记已买", lambda: self._set_status(entry_id, row_id, STATUS_ORDERED))
            self._add_action_button(layout, "找替代", lambda: self._show_replacements(entry_id, row_id))
        elif status == STATUS_ORDERED:
            self._add_action_button(layout, "标记没买到", lambda: self._set_status(entry_id, row_id, STATUS_CANCELLED))
        elif status == STATUS_RECEIVED:
            self._add_action_button(layout, "撤销入库", lambda: self._set_status(entry_id, row_id, STATUS_ORDERED))
        else:
            self._add_action_button(layout, "替代建议", lambda: self._show_replacements(entry_id, row_id))
            self._add_action_button(layout, "按原项重购", lambda: self._set_status(entry_id, row_id, STATUS_PENDING))
        return wrap

    @staticmethod
    def _add_action_button(layout: QHBoxLayout, text: str, callback) -> None:
        button = QPushButton(text)
        button.setMinimumHeight(34)
        button.clicked.connect(lambda _checked=False: callback())
        layout.addWidget(button)

    def _reload(self) -> None:
        self._payload = load_purchase_batch(self._path)
        self._refresh_header()
        self._rebuild_table()
        self.changed.emit()

    def _set_status(self, entry_id: str, row_id: str, status: str) -> None:
        try:
            set_purchase_batch_material_status(self._path, entry_id, row_id, status)
        except (OSError, ValueError) as exc:
            show_toast(self, f"状态保存失败：{exc}", style="warning")
            return
        self._reload()

    def _show_replacements(self, entry_id: str, row_id: str) -> None:
        options, target_text = purchase_batch_replacement_options(
            self._payload, entry_id, row_id
        )
        if not options:
            show_toast(self, target_text, style="warning")
            return
        dialog = PurchaseReplacementDialog(
            self,
            options=options,
            target_text=target_text,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        option = dialog.chosen_option()
        if option is None:
            return
        try:
            apply_purchase_batch_replacement(self._path, entry_id, row_id, option)
        except (OSError, ValueError) as exc:
            show_toast(self, f"替代方案保存失败：{exc}", style="warning")
            return
        show_toast(self, "已采用安全替代范围；购买成功后请标记已买", style="success")
        self._reload()

    def _toggle_all_ordered(self) -> None:
        try:
            changed, target = toggle_all_purchase_batch_materials_ordered(self._path)
        except (OSError, ValueError) as exc:
            show_toast(self, f"批量状态保存失败：{exc}", style="warning")
            return
        if target == STATUS_ORDERED:
            message = f"已将 {changed} 件材料标记为待入库"
        else:
            message = f"已撤销 {changed} 件材料的已买标记"
        show_toast(self, message, style="success" if changed else "info")
        self._reload()

    def _reconcile(self) -> None:
        profile_id = str(self._payload.get("profile_id") or "")
        try:
            result = reconcile_all_purchase_records_for_profile(
                profile_id,
                load_profile_inventory_items(profile_id),
            )
        except Exception as exc:
            show_toast(self, f"库存核对失败：{exc}", style="warning")
            return
        show_toast(
            self,
            f"新入库 {int(result.get('matched') or 0)} 件，仍待入库 {int(result.get('waiting') or 0)} 件",
            style="success" if result.get("matched") else "info",
        )
        self._reload()

    def _delete(self) -> None:
        if not ask_confirmation(
            self,
            "删除采购批次",
            f"确定删除“{self._payload.get('name') or '未命名批次'}”及其入库记录？",
        ):
            return
        try:
            delete_purchase_batch(self._path)
        except (OSError, ValueError) as exc:
            show_toast(self, f"删除失败：{exc}", style="warning")
            return
        self.deleted.emit()
