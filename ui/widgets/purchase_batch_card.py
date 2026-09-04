from __future__ import annotations

import copy
import html
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

from core.alchemy_calc import (
    tradeup_average_normalized_float32,
    tradeup_product_wear_float32,
)
from core.alchemy_quality import (
    get_pid_map,
    get_template_from_goods_name,
)
from core.data_utils import SkinInstance
from core.platform_links import MARKETPLACES, links_for_template, marketplace_by_key
from core.purchase_batches import (
    apply_purchase_batch_replacement,
    build_purchase_batch_recipe_tradeup_plan,
    delete_purchase_batch,
    load_purchase_batch,
    purchase_batch_alchemy_status_text,
    purchase_batch_recipe_tradeup_readiness,
    purchase_batch_replacement_options,
    purchase_batch_summary,
    reconcile_all_purchase_records_for_profile,
    refresh_purchase_batch_alchemy_ready_at,
    resolve_purchase_batch_inventory_departure,
    set_purchase_batch_recipe_tradeup_completed,
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
from core.saved_recipes import format_recipe_summary_line
from ui.dialogs.purchase_replacement_dialog import PurchaseReplacementDialog
from ui.feedback import ask_confirmation
from ui.icons import expand_section_triangle_icon
from ui.widgets.toast import show_toast
from ui.widgets.recipe_result_group import build_recipe_product_table


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
    "待确认离库": "missing_review",
    "需补购": STATUS_CANCELLED,
}
_PURCHASE_PLATFORM_SHORT_LABELS = {
    "buff": "BUFF",
    "yyyp": "悠悠",
    "c5": "C5",
    "eco": "ECO",
    "steam": "Steam",
    "steam_inventory": "库存",
}


def _platform_key_from_purchase_url(url: str) -> str:
    host = str(url or "").lower()
    if "buff.163.com" in host:
        return "buff"
    if "youpin898.com" in host:
        return "yyyp"
    if "c5game.com" in host:
        return "c5"
    if "ecosteam.cn" in host:
        return "eco"
    if "steamcommunity.com" in host:
        return "steam"
    return ""


def _purchase_platform_label(platform: object, url: str = "") -> str:
    key = str(platform or "").strip().lower()
    if not key:
        key = _platform_key_from_purchase_url(url)
    if key in _PURCHASE_PLATFORM_SHORT_LABELS:
        return _PURCHASE_PLATFORM_SHORT_LABELS[key]
    market = marketplace_by_key(key)
    if market is not None:
        return market.name
    return "打开"


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


def _purchase_recipe_result_snapshot(entry: dict) -> dict:
    """Rebuild output wear/probabilities from the recipe's current materials."""
    recipe = copy.deepcopy(
        entry.get("recipe") if isinstance(entry.get("recipe"), dict) else {}
    )
    substrates = recipe.get("substrates_display") or []
    pairs = []
    for material in entry.get("materials") or []:
        if not isinstance(material, dict):
            continue
        try:
            substrate = substrates[int(material.get("substrate_index"))]
        except (IndexError, TypeError, ValueError):
            substrate = {}
        substrate = substrate if isinstance(substrate, dict) else {}
        replacement = material.get("replacement")
        replacement = replacement if isinstance(replacement, dict) else {}
        name = str(
            replacement.get("name")
            or substrate.get("name")
            or material.get("name")
            or ""
        )
        template = get_template_from_goods_name(name)
        if template is None:
            continue
        raw_wear = (
            material.get("matched_float")
            if str(material.get("status") or "") == STATUS_RECEIVED
            and material.get("matched_float") is not None
            else replacement.get("manual_wear")
            if replacement.get("manual_wear") is not None
            else substrate.get("float_value", material.get("float_value", 0))
        )
        try:
            pairs.append((template, float(raw_wear)))
        except (TypeError, ValueError):
            continue

    materials_count = sum(
        1 for material in entry.get("materials") or [] if isinstance(material, dict)
    )
    if pairs and len(pairs) == materials_count:
        average = tradeup_average_normalized_float32(pairs)
        saved_prices: dict[str, object] = {}
        for product in recipe.get("products_display") or []:
            if not isinstance(product, dict):
                continue
            template = get_template_from_goods_name(str(product.get("name") or ""))
            if template is not None:
                saved_prices[str(template.paint_index)] = product.get("price", 0)
        product_rows: dict[tuple[str, float], dict] = {}
        pid_map = get_pid_map()
        for template, _wear in pairs:
            upper_ids = template.upper_skins or []
            if not upper_ids:
                continue
            probability = (1.0 / len(pairs)) / len(upper_ids)
            for product_id in upper_ids:
                product_template = pid_map.get(str(product_id))
                if product_template is None:
                    continue
                output_wear = tradeup_product_wear_float32(
                    average,
                    product_template,
                )
                key = (str(product_id), float(output_wear))
                if key not in product_rows:
                    name = (
                        f"{product_template.weapon_name} | {product_template.skin_name}"
                        if product_template.skin_name
                        else product_template.weapon_name
                    )
                    appearance = SkinInstance.get_appearance(output_wear)
                    if appearance and "|" in name:
                        name = f"{name}（{appearance}）"
                    product_rows[key] = {
                        "name": name,
                        "float_value": float(output_wear),
                        "prob": 0.0,
                        "weapon_box": "、".join(product_template.weapon_box_name or []),
                        "price": saved_prices.get(str(product_id), 0),
                    }
                product_rows[key]["prob"] += probability
        if product_rows:
            recipe["products_display"] = list(product_rows.values())
            recipe["avg_nfv"] = float(average)

    cost = float(_planned_recipe_cost(entry) or 0.0)
    products = [
        product
        for product in recipe.get("products_display") or []
        if isinstance(product, dict)
    ]
    expectation = sum(
        float(product.get("prob") or 0) * _valid_price(product.get("price"))
        for product in products
        if _valid_price(product.get("price")) is not None
    )
    recipe["cost"] = cost
    recipe["expectation"] = expectation
    recipe["rate"] = expectation / cost - 1.0 if cost > 0 else 0.0
    recipe["break_even_rate"] = sum(
        float(product.get("prob") or 0)
        for product in products
        if float(product.get("price") or 0) > cost
    )
    return recipe


class PurchaseBatchCard(QFrame):
    changed = Signal()
    deleted = Signal()
    change_account_requested = Signal(object)
    simulate_tradeup_requested = Signal(object)

    def __init__(
        self,
        path: Path,
        payload: dict,
        parent: QWidget | None = None,
        *,
        expanded: bool = False,
        ready_recipe_ids: set[str] | None = None,
        compact_archive: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("recipeManageRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._path = path
        self._payload = payload
        self._expanded = bool(expanded)
        self._ready_recipe_ids = (
            {str(value) for value in ready_recipe_ids}
            if ready_recipe_ids is not None
            else None
        )
        self._compact_archive = bool(compact_archive)
        self._table: QTableWidget | None = None
        self._tables: list[QTableWidget] = []
        self._product_tables: list[QTableWidget] = []
        self._detail_widgets: list[QWidget] = []
        self._recipe_group_buttons: list[QPushButton] = []
        self._recipe_tradeup_buttons: list[QPushButton] = []
        self._recipe_one_click_buttons: list[QPushButton] = []
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
        if self._compact_archive:
            self._reconcile_button.hide()
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
        if self._compact_archive:
            filter_label.hide()
            self._filter.hide()
            self._mark_all_button.hide()
            change_account.hide()
        actions.addStretch(1)
        controls_grid.addLayout(actions, 1, 0)
        root.addLayout(controls_grid)
        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        workflow_hint = QLabel(
            "采购流程：逐件“打开”购买 → 购买成功后“标记已买” → "
            "刷新对应 Steam 库存后“核对库存” → 材料齐全后可“一键汰换”。"
            "未到账的材料可标记为“没买到”，再筛选“需补购”查看原项重购或安全替代建议。",
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
        batch_name = str(self._payload.get("name") or "未命名采购批次")
        alchemy_text = purchase_batch_alchemy_status_text(self._payload)
        if alchemy_text:
            self._title_label.setTextFormat(Qt.TextFormat.RichText)
            self._title_label.setText(
                f'{html.escape(batch_name)}'
                f'&nbsp;&nbsp;&nbsp;&nbsp;'
                f'<span style="color:#f59e0b">{html.escape(alchemy_text)}</span>'
            )
        else:
            self._title_label.setTextFormat(Qt.TextFormat.PlainText)
            self._title_label.setText(batch_name)
        self._summary_label.setText(
            f"账号：{self._payload.get('account_name') or 'Steam'} · "
            f"{summary['recipes']} 个配方 / {summary['total']} 件 · "
            f"已入库 {summary[STATUS_RECEIVED]} · 待入库 {summary[STATUS_ORDERED]} · "
            f"待购买 {summary[STATUS_PENDING]} · 需补购 {summary[STATUS_CANCELLED]} · "
            f"待确认离库 {summary['missing_review']} · "
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
                if wanted == "missing_review" and not material.get(
                    "inventory_missing_since"
                ):
                    continue
                if (
                    wanted
                    and wanted not in {"not_received", "missing_review"}
                    and status != wanted
                ):
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
        self._product_tables.clear()
        self._recipe_group_buttons.clear()
        self._recipe_tradeup_buttons.clear()
        self._recipe_one_click_buttons.clear()
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
        missing_review = 0
        for material in entry.get("materials") or []:
            if isinstance(material, dict):
                status = str(material.get("status") or STATUS_PENDING)
                if status in status_counts:
                    status_counts[status] += 1
                if status == STATUS_RECEIVED and material.get(
                    "inventory_missing_since"
                ):
                    missing_review += 1
        summary = QLabel(
            f"{len(entry.get('materials') or [])}件 · "
            f"已入库{status_counts[STATUS_RECEIVED]} · "
            f"待入库{status_counts[STATUS_ORDERED]} · "
            f"待购买{status_counts[STATUS_PENDING]} · "
            f"需补购{status_counts[STATUS_CANCELLED]} · "
            f"待确认离库{missing_review} · "
            f"成本 ¥{float(_planned_recipe_cost(entry) or 0):.2f}",
            header,
        )
        summary.setObjectName("alchemyStep1Hint")
        header_layout.addWidget(summary, 1, Qt.AlignmentFlag.AlignVCenter)
        tradeup_completed = bool(entry.get("tradeup_completed"))
        ready, readiness_reason = purchase_batch_recipe_tradeup_readiness(entry)
        one_click_button = QPushButton("模拟并汰换", header)
        one_click_button.setObjectName("purchaseBatchOneClickTradeupBtn")
        one_click_button.setFixedHeight(32)
        one_click_button.setMinimumWidth(88)
        live_ready = ready and (
            self._ready_recipe_ids is None or entry_id in self._ready_recipe_ids
        )
        one_click_button.setEnabled(live_ready)
        one_click_button.setToolTip(
            readiness_reason
            if live_ready or not ready
            else "请前往“可炼金配方”刷新并确认库存及 CD"
        )
        one_click_button.clicked.connect(
            lambda _checked=False, recipe_id=entry_id: self._request_tradeup_simulation(
                recipe_id
            )
        )
        self._recipe_one_click_buttons.append(one_click_button)
        header_layout.addWidget(
            one_click_button,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        tradeup_button = QPushButton(
            "已汰换" if tradeup_completed else "未汰换",
            header,
        )
        tradeup_button.setObjectName("purchaseBatchTradeupStateBtn")
        tradeup_button.setProperty("completed", tradeup_completed)
        tradeup_button.setCheckable(True)
        tradeup_button.setChecked(tradeup_completed)
        tradeup_button.setFixedHeight(32)
        tradeup_button.setMinimumWidth(88)
        executed_locally = isinstance(entry.get("tradeup_execution"), dict)
        tradeup_button.setEnabled(not executed_locally)
        tradeup_button.setToolTip(
            "一键汰换成功记录不可撤销"
            if executed_locally
            else "已汰换的配方不再校验其材料是否仍在 Steam 库存中"
        )
        tradeup_button.clicked.connect(
            lambda checked, recipe_id=entry_id: self._set_tradeup_completed(
                recipe_id,
                checked,
            )
        )
        self._recipe_tradeup_buttons.append(tradeup_button)
        header_layout.addWidget(
            tradeup_button,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        root.addWidget(header)
        content = QFrame(group)
        content.setObjectName("alchemyTableFrame")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 8, 10, 10)
        table = self._build_material_table(entry, rows, content)
        content_layout.addWidget(table)
        result_recipe = _purchase_recipe_result_snapshot(entry)
        products = result_recipe.get("products_display") or []
        product_label = QLabel("产物", content)
        product_label.setObjectName("alchemyProductName")
        content_layout.addWidget(product_label)
        if products:
            product_summary = QLabel(
                format_recipe_summary_line(result_recipe),
                content,
            )
            product_summary.setObjectName("purchaseBatchProductSummary")
            product_summary.setWordWrap(True)
            content_layout.addWidget(product_summary)
            product_table = build_recipe_product_table(
                content,
                result_recipe,
                cost=float(result_recipe.get("cost") or 0),
            )
            self._product_tables.append(product_table)
            content_layout.addWidget(product_table)
        else:
            empty_products = QLabel(
                "该配方没有可用的产物明细",
                content,
            )
            empty_products.setObjectName("alchemyStep1Hint")
            empty_products.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content_layout.addWidget(empty_products)
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
            status_label = (
                "待确认离库"
                if status == STATUS_RECEIVED and material.get("inventory_missing_since")
                else _STATUS_LABELS.get(status, status)
            )
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
                status_label,
                actual,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(align)
                if column == 4:
                    item.setForeground(
                        QColor(
                            "#f59e0b"
                            if status == STATUS_RECEIVED
                            and material.get("inventory_missing_since")
                            else "#10b981"
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
        platform = str(substrate.get("platform") or material.get("platform") or "")
        replacement = material.get("replacement")
        if isinstance(replacement, dict):
            template = get_template_from_goods_name(str(replacement.get("name") or ""))
            label = _purchase_platform_label(platform)
            button = QPushButton(label, wrap)
            button.setMinimumHeight(34)
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
                    action = menu.addAction(_purchase_platform_label(market.key))
                    action.triggered.connect(
                        lambda _checked=False, value=url: QDesktopServices.openUrl(QUrl(value))
                    )
                button.setMenu(menu)
                button.setEnabled(not menu.isEmpty())
        else:
            url = str(substrate.get("purchase_link") or "")
            label = _purchase_platform_label(platform, url)
            button = QPushButton(label, wrap)
            button.setMinimumHeight(34)
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
            self._add_action_button(
                layout, "找替代", lambda: self._show_replacements(entry_id, row_id)
            )
            self._add_action_button(
                layout, "标记没买到", lambda: self._set_status(entry_id, row_id, STATUS_CANCELLED)
            )
        elif status == STATUS_RECEIVED:
            if material.get("inventory_missing_since"):
                self._add_action_button(
                    layout,
                    "卖家撤回",
                    lambda: self._resolve_departure(entry_id, row_id, True),
                )
                self._add_action_button(
                    layout,
                    "正常离库",
                    lambda: self._resolve_departure(entry_id, row_id, False),
                )
            else:
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

    def _set_tradeup_completed(self, entry_id: str, completed: bool) -> None:
        try:
            changed = set_purchase_batch_recipe_tradeup_completed(
                self._path,
                entry_id,
                completed,
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"汰换状态保存失败：{exc}", style="warning")
            self._reload()
            return
        if changed:
            show_toast(
                self,
                "已标记为已汰换，后续库存核对将忽略此配方"
                if completed
                else "已恢复为未汰换，后续将继续核对库存",
                style="success" if completed else "info",
            )
        self._reload()

    def _request_tradeup_simulation(self, entry_id: str) -> None:
        try:
            plan = build_purchase_batch_recipe_tradeup_plan(self._path, entry_id)
        except (OSError, ValueError) as exc:
            show_toast(self, str(exc), style="warning")
            self._reload()
            return
        self.simulate_tradeup_requested.emit(plan)

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

    def _resolve_departure(
        self,
        entry_id: str,
        row_id: str,
        seller_reversed: bool,
    ) -> None:
        try:
            changed = resolve_purchase_batch_inventory_departure(
                self._path,
                entry_id,
                row_id,
                seller_reversed=seller_reversed,
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"离库状态保存失败：{exc}", style="warning")
            return
        if changed:
            show_toast(
                self,
                "已转为需补购" if seller_reversed else "已记录为正常离库",
                style="warning" if seller_reversed else "success",
            )
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
            refresh_purchase_batch_alchemy_ready_at(self._path)
        except Exception as exc:
            show_toast(self, f"库存核对失败：{exc}", style="warning")
            return
        batch_key = str(self._path.resolve())
        matched = int((result.get("matched_by_path") or {}).get(batch_key, 0))
        waiting = int((result.get("waiting_by_path") or {}).get(batch_key, 0))
        missing = int((result.get("missing_by_path") or {}).get(batch_key, 0))
        message = f"本批次新入库 {matched} 件，仍待入库 {waiting} 件"
        if missing:
            message += f"；{missing} 件待确认离库"
        show_toast(
            self,
            message,
            style="warning" if missing else "success" if matched else "info",
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
