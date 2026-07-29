"""可折叠的配方结果组"""

import copy
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QHeaderView,
    QAbstractScrollArea,
    QAbstractItemView,
    QSizePolicy,
    QPushButton,
    QWidget,
)

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics

from config import FETCH_PLATFORM_LOGO_PATHS
from core.saved_recipes import (
    SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY,
    SUBSTRATE_ALCHEMY_META_LOCKED_KEY,
    format_recipe_summary_line,
    update_recipe_recipe_dict,
)

from ..icons import expand_section_triangle_icon

from .collapsible_group import AlchemyRecipeRowHoverTableWidget

_DISCLOSURE_ICON_PX = 14
from .purchase_qr_label import PurchaseActionCell, PurchaseGoButtonCell, QrSlot


def _platform_display_name(platform: object) -> str:
    p = str(platform or "").strip().lower()
    if p in {"inventory", "steam_inventory"}:
        return "库存"
    return str(platform or "")


def _product_row_price(p: dict) -> float:
    x = p.get("price", 0)
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _wear_float_selectable_cell(
    parent: QWidget,
    fv: object,
    *,
    text_color: str | None = None,
) -> QWidget:
    """与库存页卡片磨损数值一致：可鼠标/键盘选取，I 形光标。"""
    text = f"{fv:.18f}" if isinstance(fv, (int, float)) else str(fv)
    wrap = QWidget(parent)
    wrap.setAutoFillBackground(False)
    wrap.setAttribute(Qt.WA_TranslucentBackground, True)
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lb = QLabel(text)
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lb.setAutoFillBackground(False)
    lb.setAttribute(Qt.WA_TranslucentBackground, True)
    lb.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    lb.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    lb.setCursor(Qt.CursorShape.IBeamCursor)
    if text_color is not None:
        lb.setStyleSheet(f"color: {text_color}; background: transparent;")
    lay.addStretch(1)
    lay.addWidget(lb, 0, Qt.AlignmentFlag.AlignVCenter)
    lay.addStretch(1)
    return wrap


class RecipeResultGroup(QFrame):
    """可折叠的配方结果组 - 标题含成本、期望、收益率、保本率、产物归一化磨损；表内含产物标价。"""

    save_requested = Signal(int, dict)  # rank, recipe（深拷贝）

    def __init__(
        self,
        rank: int,
        recipe: dict,
        parent=None,
        *,
        enable_save: bool = False,
        recipe_storage_path: Path | None = None,
        expand_enabled: bool = True,
        get_substrate_action_state: Callable[[QrSlot], str] | None = None,
        set_substrate_action_state: Callable[[QrSlot, str], None] | None = None,
        manage_substrate_disk_actions: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("alchemyGroup")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._expand_enabled = expand_enabled
        self._expanded = False
        self._rank = rank
        self._recipe = recipe
        self._recipe_storage_path = recipe_storage_path
        self._purchase_cells: list[PurchaseGoButtonCell] = []
        self._action_cells: list[PurchaseActionCell] = []
        self._get_substrate_action_state = get_substrate_action_state
        self._set_substrate_action_state = set_substrate_action_state
        self._manage_substrate_disk_actions = bool(
            manage_substrate_disk_actions and recipe_storage_path is not None
        )
        self._show_substrate_actions = (
            self._manage_substrate_disk_actions
            or (
                self._get_substrate_action_state is not None
                and self._set_substrate_action_state is not None
            )
        )

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

        self.arrow_label = QLabel(self)
        self.arrow_label.setObjectName("alchemyGroupArrow")
        self.arrow_label.setFixedSize(_DISCLOSURE_ICON_PX, _DISCLOSURE_ICON_PX)
        header_layout.addWidget(self.arrow_label, 0, Qt.AlignVCenter)

        cost = recipe.get("cost", 0)
        title = format_recipe_summary_line(recipe)
        substrate_quality = str(recipe.get("substrate_quality") or "").strip()
        if substrate_quality:
            group_label = substrate_quality
            if recipe.get("substrate_stat_trak"):
                group_label += " · StatTrak"
            title = f"【{group_label}】{title}"
        self.title_label = QLabel(title)
        self.title_label.setObjectName("alchemyGroupTitle")
        self._rate_color = "#10b981" if recipe.get("rate", 0) >= 0 else "#ef4444"
        self.title_label.setStyleSheet(f"color: {self._rate_color};")
        header_layout.addWidget(self.title_label, 1, Qt.AlignVCenter)
        if enable_save:
            self.save_btn = QPushButton("保存配方")
            self.save_btn.setObjectName("alchemySelectFileBtn")
            self.save_btn.setCursor(Qt.PointingHandCursor)
            self.save_btn.clicked.connect(self._on_save_clicked)
            header_layout.addWidget(self.save_btn, 0, Qt.AlignVCenter)
        else:
            self.save_btn = None
        main_layout.addWidget(self.header)

        self.content_frame = QFrame(self)
        self.content_frame.setObjectName("alchemyTableFrame")
        self.content_frame.setVisible(False)
        content_layout = QVBoxLayout(self.content_frame)
        content_layout.setContentsMargins(16, 12, 16, 12)
        content_layout.setSpacing(12)

        align = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter

        sub_label = QLabel("底物")
        sub_label.setObjectName("alchemyProductName")
        content_layout.addWidget(sub_label)
        sub_table = AlchemyRecipeRowHoverTableWidget(self.content_frame)
        sub_table.setObjectName("alchemyTable")
        sub_table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        sub_headers = ["饰品", "磨损度", "价格", "武器箱/收藏品", "数据来源", "购买链接"]
        if self._show_substrate_actions:
            sub_headers.append("操作")
        sub_table.setColumnCount(len(sub_headers))
        sub_table.setHorizontalHeaderLabels(sub_headers)
        sub_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        sub_table.setColumnWidth(0, 268)
        sub_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        sub_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        _pfm = QFontMetrics(sub_table.font())
        _price_col_w = _pfm.horizontalAdvance("99999.99") + 20
        sub_table.setColumnWidth(2, max(90, _price_col_w))
        sub_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        sub_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        _src_col_w = QFontMetrics(sub_table.font()).horizontalAdvance("数据来源") + 20
        sub_table.setColumnWidth(4, max(92, _src_col_w))
        sub_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        sub_table.setColumnWidth(5, 104)
        if self._show_substrate_actions:
            sub_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Fixed)
            sub_table.setColumnWidth(6, 92)
        sub_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_table.verticalHeader().setVisible(False)
        sub_table.verticalHeader().setDefaultSectionSize(50)
        sub_table.verticalHeader().setMinimumSectionSize(46)
        sub_table.setEditTriggers(AlchemyRecipeRowHoverTableWidget.NoEditTriggers)
        substrates = recipe.get("substrates_display", [])
        qr_slots: list[QrSlot] = []
        sub_table.setRowCount(len(substrates))
        qr_row_i = 0
        for row, s in enumerate(substrates):
            it0 = QTableWidgetItem(s.get("name", ""))
            it0.setTextAlignment(align)
            sub_table.setItem(row, 0, it0)
            fv = s.get("float_value", 0)
            sub_table.setCellWidget(
                row,
                1,
                _wear_float_selectable_cell(sub_table, fv),
            )
            pr = s.get("price", 0)
            it2 = QTableWidgetItem(f"{pr:.2f}" if isinstance(pr, (int, float)) else str(pr))
            it2.setTextAlignment(align)
            sub_table.setItem(row, 2, it2)
            it3 = QTableWidgetItem(s.get("weapon_box", ""))
            it3.setTextAlignment(align)
            sub_table.setItem(row, 3, it3)
            it4 = QTableWidgetItem(_platform_display_name(s.get("platform", "")))
            it4.setTextAlignment(align)
            sub_table.setItem(row, 4, it4)
            raw_pl = s.get("purchase_link")
            pl = raw_pl.strip() if isinstance(raw_pl, str) else ""
            plat = str(s.get("platform", "") or "").strip().lower()
            logo = FETCH_PLATFORM_LOGO_PATHS.get(plat)
            row_slot = QrSlot(
                url=pl,
                logo_path=logo,
                name=str(s.get("name", "")),
                platform=plat,
                float_value=s.get("float_value"),
                price=s.get("price"),
            )
            if pl:
                qr_slots.append(row_slot)
                cell = PurchaseGoButtonCell(
                    pl,
                    qr_slots=qr_slots,
                    qr_index=qr_row_i,
                    recipe=self._recipe,
                    on_mark_viewed=self._mark_purchase_viewed,
                    anchor_parent=sub_table,
                    parent=sub_table,
                )
                self._purchase_cells.append(cell)
                qr_row_i += 1
                sub_table.setCellWidget(row, 5, cell)
            else:
                it5 = QTableWidgetItem("-")
                it5.setTextAlignment(align)
                sub_table.setItem(row, 5, it5)
            if self._show_substrate_actions:
                if self._manage_substrate_disk_actions:
                    ri = row

                    def _disk_get(_slot: QrSlot, _ri: int = ri) -> str:
                        return self._disk_substrate_action_state(_ri)

                    def _disk_set(_slot: QrSlot, ts: str, _ri: int = ri) -> None:
                        self._disk_apply_substrate_action_state(_ri, ts)

                    get_s = _disk_get
                    set_s = _disk_set
                else:
                    get_s = self._get_substrate_action_state
                    set_s = self._set_substrate_action_state
                action_cell = PurchaseActionCell(
                    row_slot,
                    get_slot_action_state=get_s,
                    set_slot_action_state=set_s,
                    on_slot_action_changed=self._refresh_action_cells,
                    parent=sub_table,
                )
                self._action_cells.append(action_cell)
                sub_table.setCellWidget(row, 6, action_cell)
            else:
                it6 = QTableWidgetItem("-")
                it6.setTextAlignment(align)
                sub_table.setItem(row, 6, it6)
        content_layout.addWidget(sub_table)

        prod_label = QLabel("产物")
        prod_label.setObjectName("alchemyProductName")
        content_layout.addWidget(prod_label)
        prod_table = AlchemyRecipeRowHoverTableWidget(self.content_frame)
        # 独立 objectName：主题 QSS 里 #alchemyTable::item 强写了 color，会盖住 setForeground
        prod_table.setObjectName("alchemyRecipeProductTable")
        prod_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        prod_table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        prod_table.setColumnCount(5)
        prod_table.setHorizontalHeaderLabels(["饰品", "磨损度", "武器箱/收藏品", "价格", "概率"])
        prod_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        prod_table.setColumnWidth(0, 280)
        for col in range(1, 5):
            prod_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Stretch)
        prod_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        prod_table.verticalHeader().setVisible(False)
        prod_table.verticalHeader().setDefaultSectionSize(50)
        prod_table.setEditTriggers(AlchemyRecipeRowHoverTableWidget.NoEditTriggers)
        products = sorted(
            list(recipe.get("products_display", [])),
            key=_product_row_price,
            reverse=True,
        )
        prod_table.setRowCount(len(products))
        cost_f = float(cost) if isinstance(cost, (int, float)) else 0.0
        profit_brush = QBrush(QColor("#10b981"))
        loss_brush = QBrush(QColor("#ef4444"))
        for row, p in enumerate(products):
            row_brush = profit_brush if _product_row_price(p) > cost_f else loss_brush
            it0 = QTableWidgetItem(p.get("name", ""))
            it0.setTextAlignment(align)
            it0.setForeground(row_brush)
            prod_table.setItem(row, 0, it0)
            fv = p.get("float_value", 0)
            prod_table.setCellWidget(
                row,
                1,
                _wear_float_selectable_cell(
                    prod_table,
                    fv,
                    text_color=row_brush.color().name(),
                ),
            )
            it2 = QTableWidgetItem(p.get("weapon_box", ""))
            it2.setTextAlignment(align)
            it2.setForeground(row_brush)
            prod_table.setItem(row, 2, it2)
            prc = p.get("price", 0)
            pval = _product_row_price(p)
            profit = pval - cost_f
            if isinstance(prc, (int, float)):
                price_cell = f"{pval:.2f} ({profit:+.2f})"
            else:
                price_cell = f"{prc} ({profit:+.2f})"
            itp = QTableWidgetItem(price_cell)
            itp.setTextAlignment(align)
            itp.setForeground(row_brush)
            prod_table.setItem(row, 3, itp)
            prob = p.get("prob", 0)
            it3 = QTableWidgetItem(f"{prob:.2%}" if isinstance(prob, (int, float)) else str(prob))
            it3.setTextAlignment(align)
            it3.setForeground(row_brush)
            prod_table.setItem(row, 4, it3)
        content_layout.addWidget(prod_table)

        main_layout.addWidget(self.content_frame)
        self.header.mousePressEvent = self._on_header_clicked
        self._update_disclosure_arrow()

    def _disk_substrate_action_state(self, row_i: int) -> str:
        subs = self._recipe.get("substrates_display")
        if not isinstance(subs, list) or row_i < 0 or row_i >= len(subs):
            return "neutral"
        s = subs[row_i]
        if not isinstance(s, dict):
            return "neutral"
        if bool(s.get(SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY)):
            return "excluded"
        if bool(s.get(SUBSTRATE_ALCHEMY_META_LOCKED_KEY)):
            return "locked"
        return "neutral"

    def _disk_apply_substrate_action_state(self, row_i: int, target_state: str) -> None:
        subs = self._recipe.setdefault("substrates_display", [])
        if not isinstance(subs, list) or row_i < 0 or row_i >= len(subs):
            return
        s = subs[row_i]
        if not isinstance(s, dict):
            return
        if target_state == "excluded":
            s[SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY] = True
            s.pop(SUBSTRATE_ALCHEMY_META_LOCKED_KEY, None)
        elif target_state == "locked":
            s[SUBSTRATE_ALCHEMY_META_LOCKED_KEY] = True
            s[SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY] = False
        else:
            s.pop(SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY, None)
            s.pop(SUBSTRATE_ALCHEMY_META_LOCKED_KEY, None)
        if self._recipe_storage_path is not None:
            try:
                update_recipe_recipe_dict(self._recipe_storage_path, self._recipe)
            except (OSError, ValueError):
                pass

    def _update_disclosure_arrow(self) -> None:
        icon = expand_section_triangle_icon(
            self._expanded,
            size_px=_DISCLOSURE_ICON_PX,
            fill_color=self._rate_color,
        )
        self.arrow_label.setPixmap(
            icon.pixmap(_DISCLOSURE_ICON_PX, _DISCLOSURE_ICON_PX)
        )

    def mousePressEvent(self, event) -> None:
        # 批量管理下不可展开：header 对左键 ignore 后事件先到本 Group，需再 ignore 才能交给外层配方行（勾选 / 滑动多选）
        if (
            not self._expand_enabled
            and event.button() == Qt.MouseButton.LeftButton
        ):
            event.ignore()
            return
        super().mousePressEvent(event)

    def _mark_purchase_viewed(self, url_key: str) -> None:
        pv = self._recipe.setdefault("purchase_viewed", {})
        if not isinstance(pv, dict):
            self._recipe["purchase_viewed"] = {}
            pv = self._recipe["purchase_viewed"]
        pv[url_key] = True
        if self._recipe_storage_path is not None:
            try:
                update_recipe_recipe_dict(self._recipe_storage_path, self._recipe)
            except (OSError, ValueError):
                pass
        self._refresh_purchase_cells()

    def _refresh_purchase_cells(self) -> None:
        for c in self._purchase_cells:
            c.apply_viewed_state()

    def _refresh_action_cells(self, _slot: QrSlot | None = None) -> None:
        del _slot
        for c in self._action_cells:
            c.refresh_state()

    def _on_save_clicked(self):
        self.save_requested.emit(self._rank, copy.deepcopy(self._recipe))

    def _on_header_clicked(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._expand_enabled:
                self.toggle()
            else:
                event.ignore()
            return
        QFrame.mousePressEvent(self.header, event)

    def set_save_button_text(self, text: str) -> None:
        if self.save_btn is not None:
            self.save_btn.setText(text)

    def set_expand_enabled(self, enabled: bool) -> None:
        self._expand_enabled = enabled
        if not enabled:
            self.collapse()

    def collapse(self) -> None:
        if not self._expanded:
            self.updateGeometry()
            return
        self._expanded = False
        self._update_disclosure_arrow()
        self.content_frame.setVisible(False)
        self.updateGeometry()

    def toggle(self):
        if not self._expand_enabled:
            return
        self._expanded = not self._expanded
        self._update_disclosure_arrow()
        self.content_frame.setVisible(self._expanded)
        self.updateGeometry()
