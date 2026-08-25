from __future__ import annotations

import math
import re
from decimal import Decimal

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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
from core.data_utils import SkinInstance
from core.platform_links import MARKETPLACES, links_for_template


class PurchaseReplacementDialog(QDialog):
    """Show original or same-outcome-pool replacements with feasible wear ranges."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        options: list[dict],
        target_text: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("缺口材料替代建议")
        self.setMinimumSize(1000, 560)
        self._options = options
        self._chosen: dict | None = None
        # Both normal and special-wear purchases ultimately refer to one concrete
        # market listing.  Keep the mathematically allowed interval, then narrow
        # it by the listing's exact six-decimal prefix for inventory matching.
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
            "仅显示原材料和同产物池安全替代；磨损区间按该配方其余槽位当前材料反算。"
            "替代品成交价不同会改变配方实际成本和收益率，请购买前自行核对价格。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("alchemyStep1Hint")
        root.addWidget(hint)

        self.table = QTableWidget(len(options), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["替代材料", "安全性", "允许购买磨损", "购买链接"]
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
                    f"磨损必须正好填写{self._manual_decimals}位小数，并落在允许范围内"
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
        allowed_low = float(option.get("min_wear"))
        allowed_high = float(option.get("max_wear"))
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
        self._chosen = dict(option)
        self._chosen["allowed_min_wear"] = float(option.get("min_wear"))
        self._chosen["allowed_max_wear"] = float(option.get("max_wear"))
        self._chosen["min_wear"] = match_low
        self._chosen["max_wear"] = match_high
        self._chosen["manual_wear"] = manual_wear
        self._chosen["manual_wear_decimals"] = self._manual_decimals
        self._chosen["manual_wear_match_mode"] = (
            f"decimal_prefix_{self._manual_decimals}"
        )
        self._chosen["purchase_price"] = purchase_price
        self.accept()

    def chosen_option(self) -> dict | None:
        return dict(self._chosen) if self._chosen is not None else None
