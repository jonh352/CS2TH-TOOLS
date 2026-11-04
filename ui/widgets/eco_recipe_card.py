"""ECO 合炉页：按日期展示的单炉配方和材料明细。"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.eco_furnace import purchase_recipe_for_furnace
from core.eco_publication import recipe_publication
from core.saved_recipes import format_recipe_summary_line
from ui.icons import expand_section_triangle_icon
from ui.widgets.recipe_result_group import build_recipe_product_table


class EcoRecipeCard(QFrame):
    publish_requested = Signal(object, str)
    manage_requested = Signal()
    publication_mark_requested = Signal(object, str, bool)

    def __init__(self, path: Path, batch: dict, entry: dict, parent=None, *, recipe_number=1):
        super().__init__(parent)
        self.setObjectName("recipeManageRow")
        self.path, self.batch, self.entry = path, batch, entry
        self._table = None
        self._expanded = False
        self._details = None
        self._recipe = purchase_recipe_for_furnace(batch, entry)
        publication = recipe_publication(batch, entry)
        state = publication.get("state", "unlisted")
        recipe_index = recipe_number
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(9)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(f"第 {recipe_index:02d} 炉 · {batch.get('account_name') or 'Steam'}", self)
        title.setToolTip(str(entry.get("title") or "采购配方"))
        title.setTextFormat(Qt.PlainText)
        title.setObjectName("recipeSavedTitle")
        titles.addWidget(title)
        cost = sum(float(row.get("price") or 0) for row in self._recipe.get("substrates_display") or [])
        count = len(entry.get("materials") or [])
        account = QLabel(f"{count} 件材料 · 已入库 {count} · 成本 ¥{cost:.2f}", self)
        account.setTextFormat(Qt.PlainText)
        account.setObjectName("alchemyStep1Hint")
        titles.addWidget(account)
        status_row = QHBoxLayout()
        status_label = QLabel({"unlisted": "未上合炉", "listed": "已上合炉", "submitted": "已提交合炉 · 待平台确认", "pending": "已提交请求 · 待确认", "uncertain": "提交结果待确认 · 请先查询 ECO"}[state], self)
        status_label.setObjectName("ecoPublicationStatus")
        status_label.setStyleSheet("color: #10b981; font-weight: 600;" if state == "listed" else "color: #f59e0b;" if publication else "color: #64748b;")
        status_label.setToolTip("按本地提交记录区分；此前已在 ECO 发布的旧炉可手动标记。" + (f"\n合炉 ID：{publication['furnace_id']}" if publication.get("furnace_id") else ""))
        status_row.addWidget(status_label)
        self.mark_publication_button = QPushButton("标记未上合炉" if state == "listed" else "标记已上合炉", self)
        self.mark_publication_button.setToolTip("仅修改本地分类，不创建或取消 ECO 合炉；请先核对平台实际状态。")
        self.mark_publication_button.clicked.connect(lambda: self.publication_mark_requested.emit(path, str(entry.get("id") or ""), state != "listed"))
        status_row.addWidget(self.mark_publication_button)
        if state in {"submitted", "pending", "uncertain"}:
            reset_button = QPushButton("标记未上合炉", self)
            reset_button.setToolTip("确认 ECO 没有创建成功或该炉已取消后，恢复到未上合炉分类。仅修改本地标记。")
            reset_button.clicked.connect(lambda: self.publication_mark_requested.emit(path, str(entry.get("id") or ""), False))
            status_row.addWidget(reset_button)
        status_row.addStretch(1)
        titles.addLayout(status_row)
        header.addLayout(titles, 1)
        self.publish_button = QPushButton("查看合炉" if publication else "发布合炉", self)
        self.publish_button.setObjectName("alchemySelectFileBtn")
        self.publish_button.setMinimumSize(190, 42)
        self.publish_button.setCursor(Qt.PointingHandCursor)
        self.publish_button.clicked.connect(lambda: self.manage_requested.emit() if publication else self.publish_requested.emit(path, str(entry.get("id") or "")))
        header.addWidget(self.publish_button)
        self.material_button = QPushButton(f"查看材料（{count}）", self)
        self.material_button.setObjectName("purchaseBatchViewMaterialsBtn")
        self.material_button.setMinimumSize(190, 42)
        self.material_button.clicked.connect(self._toggle_materials)
        header.addWidget(self.material_button)
        root.addLayout(header)

        self._group = QFrame(self)
        self._group.setObjectName("alchemyGroup")
        group_layout = QVBoxLayout(self._group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(0)
        recipe_header = QFrame(self._group)
        recipe_header.setObjectName("alchemyGroupHeader")
        recipe_header.setFixedHeight(44)
        recipe_row = QHBoxLayout(recipe_header)
        recipe_row.setContentsMargins(12, 0, 12, 0)
        recipe_row.setSpacing(8)
        self._recipe_toggle = QPushButton(str(recipe_index), recipe_header)
        self._recipe_toggle.setObjectName("purchaseRecipeToggleBtn")
        self._recipe_toggle.setFlat(True)
        self._recipe_toggle.setCursor(Qt.PointingHandCursor)
        self._recipe_toggle.setStyleSheet("QPushButton { text-align: left; border: none; background: transparent; }")
        self._recipe_toggle.setToolTip(str(entry.get("title") or "采购配方"))
        self._recipe_toggle.setFixedHeight(32)
        self._recipe_toggle.clicked.connect(self._toggle_details)
        recipe_row.addWidget(self._recipe_toggle)
        summary = QLabel(f"{count}件 · 已入库{count} · 待入库0 · 待购买0 · 需补购0 · 成本 ¥{cost:.2f}", recipe_header)
        summary.setObjectName("alchemyStep1Hint")
        summary.setToolTip(str(entry.get("title") or "采购配方"))
        recipe_row.addWidget(summary, 1)
        ready = QLabel("可炼金", recipe_header)
        ready.setObjectName("purchaseRecipeAlchemyTime")
        ready.setStyleSheet("color: #f59e0b;")
        recipe_row.addWidget(ready)
        group_layout.addWidget(recipe_header)
        self._group.hide()
        root.addWidget(self._group)
        self._set_triangle(False)

    def _set_triangle(self, expanded):
        self._recipe_toggle.setIcon(expand_section_triangle_icon(expanded, size_px=14, fill_color="#10b981"))

    def _build_details(self):
        content = QFrame(self._group)
        content.setObjectName("alchemyTableFrame")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 8, 10, 10)
        rows = self._recipe.get("substrates_display") or []
        materials = {int(row["substrate_index"]): row for row in self.entry.get("materials") or []}
        table = QTableWidget(len(rows), 7, content)
        table.setHorizontalHeaderLabels(["序号", "材料", "计划磨损", "采集价格", "状态", "实际入库", "Steam 资产 ID"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setMinimumSectionSize(48)
        table.verticalHeader().setDefaultSectionSize(48)
        table.setMinimumHeight(min(620, 56 + max(1, len(rows)) * 48))
        table.setMaximumHeight(620)
        for column, width in enumerate((48, 250, 180, 90, 90, 150, 160)):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.Fixed)
            table.setColumnWidth(column, width)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for index, row in enumerate(rows):
            material = materials.get(index, {})
            wear = row.get("float_value")
            planned = material.get("float_value")
            price = row.get("price")
            values = (str(index + 1), row.get("name") or "", f"{float(planned):.18f}" if planned is not None else "-",
                      f"¥{float(price):.2f}" if price is not None else "-", "已入库",
                      f"{float(wear):.18f}" if wear is not None else "-", str(row.get("steam_assetid") or ""))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(value)
                if column == 4:
                    item.setForeground(QColor("#10b981"))
                table.setItem(index, column, item)
        self._table = table
        layout.addWidget(table)
        product_label = QLabel("产物", content)
        product_label.setObjectName("alchemyProductName")
        layout.addWidget(product_label)
        summary = QLabel(format_recipe_summary_line(self._recipe), content)
        summary.setObjectName("purchaseBatchProductSummary")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if self._recipe.get("products_display"):
            layout.addWidget(build_recipe_product_table(content, self._recipe, cost=float(self._recipe.get("cost") or 0)))
        self._group.layout().addWidget(content)
        self._details = content

    def _toggle_materials(self):
        self._expanded = not self._expanded
        if self._expanded:
            if self._details is None:
                self._build_details()
            self._details.show()
            self._set_triangle(True)
        self._group.setVisible(self._expanded)
        self.material_button.setText("收起材料" if self._expanded else f"查看材料（{len(self.entry.get('materials') or [])}）")

    def _toggle_details(self):
        if self._details is None:
            self._build_details()
        expanded = self._details.isHidden()
        self._details.setVisible(expanded)
        self._set_triangle(expanded)
