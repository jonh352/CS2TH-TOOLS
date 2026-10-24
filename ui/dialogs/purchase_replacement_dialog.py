from __future__ import annotations

import math
import re
from collections.abc import Callable
from decimal import Decimal

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.alchemy_quality import get_template_from_goods_name
from core.data_utils import LARGE_VALUE_LIST, SkinInstance
from core.platform_links import MARKETPLACES, links_for_template


class PurchaseReplacementDialog(QDialog):
    """Show original or same-outcome-pool replacements with feasible wear ranges."""

    collect_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None,
        *,
        options: list[dict],
        target_text: str,
        preview_builder: Callable[[dict], dict] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("缺口材料替代建议")
        self.setMinimumSize(1000, 560)
        self._options = options
        self._chosen: dict | None = None
        self._preview_builder = preview_builder
        # Match the listing's six-decimal prefix within its physical template
        # range. Recommendation limits only guide shopping and collection.
        self._requires_manual_wear = bool(options)
        self._manual_decimals = 6 if self._requires_manual_wear else None
        self._manual_step = (
            Decimal(1).scaleb(-self._manual_decimals)
            if self._manual_decimals is not None
            else None
        )

        root = QVBoxLayout(self)
        title = QLabel(target_text)
        title.setObjectName("recipeSavedTitle")
        root.addWidget(title)
        hint = QLabel(
            "仅显示原材料和同产物池替代；推荐磨损按其余槽位已入库、已填替代和原计划反推。"
            "输入可超出推荐范围，采用时先预览整组产物并确认。"
            "替代品成交价不同会改变配方实际成本和收益率，请购买前自行核对价格。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("alchemyStep1Hint")
        root.addWidget(hint)

        self.table = QTableWidget(len(options), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["替代材料", "产物池关系", "反推推荐磨损（仅供参考）", "购买链接"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(48)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.horizontalHeader().setStretchLastSection(False)
        wear_texts = [
            (
                f"不高于 {float(option.get('max_wear')):.18f}"
                if option.get("range_mode") == "not_higher"
                else (
                    f"{float(option.get('min_wear')):.18f} ～ "
                    f"{float(option.get('max_wear')):.18f}"
                )
            )
            for option in options
        ]
        wear_column_width = max(
            390,
            max(
                (self.table.fontMetrics().horizontalAdvance(text) + 28 for text in wear_texts),
                default=390,
            ),
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 165)
        self.table.setColumnWidth(2, wear_column_width)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        root.addWidget(self.table, 1)

        for row_index, option in enumerate(options):
            self.table.setRowHeight(row_index, 48)
            safe = bool(option.get("safe"))
            color = QColor("#10b981" if safe else "#ef4444")
            if option.get("range_mode") == "not_higher":
                wear_text = f"不高于 {float(option.get('max_wear')):.18f}"
            else:
                wear_text = (
                    f"{float(option.get('min_wear')):.18f} ～ "
                    f"{float(option.get('max_wear')):.18f}"
                )
            if option.get("recommended_available") is False:
                wear_text = "无满足原目标的推荐范围，可输入实际磨损预览"
            values = (
                str(option.get("name") or ""),
                str(option.get("relation") or ""),
                wear_text,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setForeground(color)
                if column > 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column, item)
            self.table.setCellWidget(row_index, 3, self._purchase_cell(option))

        collect_row = QHBoxLayout()
        collect_row.addWidget(QLabel("采集窗口宽度（从各材料推荐上限向下）："))
        self.collection_width = QDoubleSpinBox(self)
        self.collection_width.setDecimals(6)
        self.collection_width.setRange(0.000001, 1.0)
        self.collection_width.setValue(0.01)
        self.collection_width.setSingleStep(0.001)
        collect_row.addWidget(self.collection_width)
        self.collect_selected_button = QPushButton("采集所选材料", self)
        self.collect_all_button = QPushButton("采集原材料及全部替代", self)
        self.collect_selected_button.clicked.connect(
            lambda: self._collect_options(selected_only=True)
        )
        self.collect_all_button.clicked.connect(
            lambda: self._collect_options(selected_only=False)
        )
        collect_row.addWidget(self.collect_selected_button)
        collect_row.addWidget(self.collect_all_button)
        collect_row.addStretch(1)
        root.addLayout(collect_row)

        details = QGridLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setHorizontalSpacing(24)
        details.setVerticalSpacing(8)
        details.setColumnStretch(0, 1)
        details.setColumnStretch(1, 1)

        self.selected_name_label = QLabel("已选饰品：请从上表选择")
        self.selected_name_label.setObjectName("alchemyStep1Hint")
        details.addWidget(self.selected_name_label, 0, 0)
        self.manual_wear_hint = QLabel(
            f"库存实际磨损的小数点后前{self._manual_decimals or 6}位必须完全一致"
        )
        self.manual_wear_hint.setObjectName("alchemyStep1Hint")
        self.manual_wear_hint.setVisible(self._requires_manual_wear)
        details.addWidget(self.manual_wear_hint, 0, 1)

        price_container = QWidget(self)
        price_row = QHBoxLayout(price_container)
        price_row.setContentsMargins(0, 0, 0, 0)
        price_row.addWidget(QLabel("替代材料购买价格（元）："))
        self.purchase_price_edit = QLineEdit(self)
        self.purchase_price_edit.setPlaceholderText("例如 12.50")
        self.purchase_price_edit.setMaximumWidth(180)
        self.purchase_price_edit.setClearButtonEnabled(True)
        price_row.addWidget(self.purchase_price_edit)
        price_row.addStretch(1)
        details.addWidget(price_container, 1, 0)

        self.manual_wear_container = QWidget(self)
        wear_row = QHBoxLayout(self.manual_wear_container)
        wear_row.setContentsMargins(0, 0, 0, 0)
        display_decimals = self._manual_decimals or 6
        wear_label = QLabel(
            f"实际购买磨损（小数点后前{display_decimals}位）："
        )
        wear_row.addWidget(wear_label)
        self.manual_wear_edit = QLineEdit(self)
        example = "0.164959"
        self.manual_wear_edit.setPlaceholderText(
            f"直接截取前{display_decimals}位，例如 {example}"
        )
        self.manual_wear_edit.setMaximumWidth(200)
        self.manual_wear_edit.setClearButtonEnabled(True)
        wear_row.addWidget(self.manual_wear_edit)
        wear_row.addStretch(1)
        self.manual_wear_container.setVisible(self._requires_manual_wear)
        details.addWidget(self.manual_wear_container, 1, 1)
        root.addLayout(details)
        self.preview_error_label = QLabel(self)
        self.preview_error_label.setWordWrap(True)
        root.addWidget(self.preview_error_label)

        buttons = QDialogButtonBox(self)
        self.adopt_button = buttons.addButton(
            "采用所选安全替代",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        self.adopt_button.setEnabled(False)
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.table.itemSelectionChanged.connect(self._sync_selected_name)
        self.table.itemSelectionChanged.connect(self._sync_adopt_enabled)
        self.purchase_price_edit.textChanged.connect(self._sync_adopt_enabled)
        self.manual_wear_edit.textChanged.connect(self._sync_adopt_enabled)
        if options:
            self.table.selectRow(0)

    def _purchase_cell(self, option: dict) -> QWidget:
        wrap = QWidget(self.table)
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(6, 4, 6, 4)
        button = QPushButton("打开购买", wrap)
        button.setMinimumSize(150, 36)
        template = get_template_from_goods_name(str(option.get("name") or ""))
        if template is None:
            button.setEnabled(False)
            layout.addWidget(button, 1, Qt.AlignmentFlag.AlignVCenter)
            return wrap
        low = float(option.get("min_wear"))
        high = float(option.get("max_wear"))
        appearance = SkinInstance.get_appearance((low + high) / 2.0) or ""
        links = links_for_template(
            template,
            appearance,
            min_wear=low,
            max_wear=high,
        )
        menu = QMenu(button)
        usable = 0
        for market in MARKETPLACES:
            url = str(links.get(market.key) or "")
            if not url or url == market.home_url:
                continue
            action = menu.addAction(market.name)
            action.triggered.connect(
                lambda _checked=False, value=url: QDesktopServices.openUrl(QUrl(value))
            )
            usable += 1
        button.setMenu(menu)
        button.setEnabled(usable > 0)
        layout.addWidget(button, 1, Qt.AlignmentFlag.AlignVCenter)
        return wrap

    def _selected_option(self) -> dict | None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        index = selected[0].row()
        return self._options[index] if 0 <= index < len(self._options) else None

    def _collect_options(self, *, selected_only: bool) -> None:
        options = [self._selected_option()] if selected_only else self._options
        items = []
        for option in options:
            if (
                option is None or not option.get("safe")
                or option.get("recommended_available") is False
            ):
                continue
            high = float(option["max_wear"])
            low = max(float(option["min_wear"]), high - self.collection_width.value())
            # Marketplace goods IDs are appearance-specific; split a window
            # crossing a boundary so collection visits both goods pages.
            cuts = [low] + [value for value in LARGE_VALUE_LIST if low < value < high] + [high]
            for start, end in zip(cuts, cuts[1:]):
                upper = math.nextafter(end, -math.inf) if end != high else end
                items.append(dict(name=option["name"], min_wear=start, max_wear=upper))
        if not items:
            self.preview_error_label.setText("当前无满足原目标的推荐采集范围；可手动填写实际购买磨损后预览。")
            return
        self.collect_requested.emit({
            "title": "采购补买 · 反推磨损采集",
            "source": "purchase_replacement",
            "items": items,
        })
        self.reject()

    def _sync_selected_name(self) -> None:
        option = self._selected_option()
        name = str(option.get("name") or "") if option is not None else ""
        self.selected_name_label.setText(
            f"已选饰品：{name}"
            if name
            else "已选饰品：请从上表选择"
        )

    def _purchase_price(self) -> float | None:
        text = self.purchase_price_edit.text().strip()
        if re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d{1,2})?", text) is None:
            return None
        value = Decimal(text)
        if value <= 0:
            return None
        price = float(value)
        return price if math.isfinite(price) else None

    def _sync_adopt_enabled(self) -> None:
        self.preview_error_label.clear()
        option = self._selected_option()
        purchase_price = self._purchase_price()
        has_safe_selection = bool(option and option.get("safe"))
        if self.purchase_price_edit.text() and purchase_price is None:
            self.purchase_price_edit.setStyleSheet("border: 1px solid #ef4444;")
            self.purchase_price_edit.setToolTip("价格必须大于0，且最多保留2位小数")
        else:
            self.purchase_price_edit.setStyleSheet("")
            self.purchase_price_edit.setToolTip("")
        if not self._requires_manual_wear:
            self.adopt_button.setEnabled(
                bool(has_safe_selection and purchase_price is not None)
            )
            return
        interval = self._manual_match_interval(option)
        self.adopt_button.setEnabled(
            bool(
                has_safe_selection
                and interval is not None
                and purchase_price is not None
            )
        )
        if option is not None and option.get("safe") and self.manual_wear_edit.text():
            if interval is None:
                self.manual_wear_edit.setStyleSheet(
                    "border: 1px solid #ef4444;"
                )
                self.manual_wear_edit.setToolTip(
                    f"磨损必须正好填写{self._manual_decimals}位小数，并在饰品自身范围内；可以超出推荐范围"
                )
                return
        self.manual_wear_edit.setStyleSheet("")
        self.manual_wear_edit.setToolTip("")

    def _manual_match_interval(
        self,
        option: dict | None,
    ) -> tuple[float, float, float] | None:
        if option is None or not bool(option.get("safe")):
            return None
        if not self._requires_manual_wear or self._manual_decimals is None:
            return None
        text = self.manual_wear_edit.text().strip()
        pattern = rf"(?:0\.\d{{{self._manual_decimals}}}|1\.{'0' * self._manual_decimals})"
        if re.fullmatch(pattern, text) is None:
            return None
        value_decimal = Decimal(text)
        value = float(value_decimal)
        template = get_template_from_goods_name(str(option.get("name") or ""))
        allowed_low = float(option.get(
            "template_min_wear", template.min_float if template else 0.0
        ))
        allowed_high = float(option.get(
            "template_max_wear", template.max_float if template else 1.0
        ))
        prefix_low = value
        if text == f"1.{'0' * self._manual_decimals}":
            prefix_high = 1.0
        else:
            prefix_high = math.nextafter(
                float(value_decimal + self._manual_step),
                -math.inf,
            )
        match_low = max(allowed_low, prefix_low)
        match_high = min(allowed_high, prefix_high)
        if match_low > match_high:
            return None
        return value, match_low, match_high

    def _accept_selected(self) -> None:
        option = self._selected_option()
        purchase_price = self._purchase_price()
        if not self._requires_manual_wear:
            if (
                option is None
                or not bool(option.get("safe"))
                or purchase_price is None
            ):
                self._sync_adopt_enabled()
                return
            self._chosen = dict(option)
            self._chosen["purchase_price"] = purchase_price
            self.accept()
            return
        interval = self._manual_match_interval(option)
        if (
            option is None
            or not bool(option.get("safe"))
            or interval is None
            or purchase_price is None
        ):
            self._sync_adopt_enabled()
            return
        manual_wear, match_low, match_high = interval
        chosen = dict(option)
        chosen["allowed_min_wear"] = float(option.get("min_wear"))
        chosen["allowed_max_wear"] = float(option.get("max_wear"))
        chosen["min_wear"] = match_low
        chosen["max_wear"] = match_high
        chosen["manual_wear"] = manual_wear
        chosen["manual_wear_decimals"] = self._manual_decimals
        chosen["manual_wear_match_mode"] = (
            f"decimal_prefix_{self._manual_decimals}"
        )
        chosen["purchase_price"] = purchase_price
        if self._preview_builder is not None:
            try:
                preview = self._preview_builder(chosen)
            except (OSError, TypeError, ValueError) as exc:
                self.preview_error_label.setText(f"无法模拟：{exc}")
                return
            if not self._confirm_preview(preview):
                return
            chosen["outcome_preview_confirmed"] = True
            chosen["outcome_preview_fingerprint"] = preview["fingerprint"]
        self._chosen = chosen
        self.accept()

    def _confirm_preview(self, preview: dict) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("确认整组汰换结果")
        dialog.resize(1060, 560)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            f"整组 {preview['count']} 件材料 · 另有 {preview['estimated_count']} 件未入库，按已填替代或原计划估算。\n"
            "磨损仅填写前6位，替换后展示可能的产物磨损范围；实际入库后以完整磨损重新模拟。\n"
            f"归一化均值：{preview['original_average']:.10f} → {preview['min_average']:.10f} ～ {preview['max_average']:.10f}"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(preview["products"]), 6, dialog)
        table.setHorizontalHeaderLabels(["产物", "概率", "原配方磨损", "替换前磨损", "替换后磨损范围", "与原配方比较"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for row, product in enumerate(preview["products"]):
            values = [product["name"], f"{product['prob']:.2%}", f"{product['original_wear']:.10f}",
                      f"{product['before_wear']:.10f}", f"{product['min_wear']:.10f} ～ {product['max_wear']:.10f}", product["changes"]]
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(table, 1)
        warning = QLabel("请确认以上产物磨损变化可以接受，再保存本次替代。")
        if not preview["special_target_met"]:
            warning.setText("本次替代可能超出原特殊磨损目标。请核对以上整组结果，接受后可继续保存。")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        buttons = QDialogButtonBox(dialog)
        buttons.addButton("接受结果并采用", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("返回修改", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def chosen_option(self) -> dict | None:
        return dict(self._chosen) if self._chosen is not None else None
