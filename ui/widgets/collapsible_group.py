"""可展开/收起的商品组 - 底物数据选择用"""

import logging
from collections.abc import Callable

import numpy as np

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QSizePolicy, QCheckBox, QWidget, QApplication,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QAbstractItemView,
)
from PySide6.QtCore import QEvent, QRect, Qt, QSize
from PySide6.QtGui import QFontMetrics, QKeySequence, QPalette

from core.alchemy_quality import (
    canonical_goods_name_for_lookup,
    get_quality_from_goods_name,
    get_template_from_goods_name,
)
from core.data_utils import QUALITY_COLORS, SkinInstance

from ..icons import expand_section_triangle_icon

_DISCLOSURE_ICON_PX = 14
logger = logging.getLogger(__name__)

# 底物表复选框指示器最小边长（像素），略大于系统默认
_SUBSTRATE_CHECK_INDICATOR_MIN = 18


def _platform_display_name(platform: object) -> str:
    p = str(platform or "").strip().lower()
    if p in {"inventory", "steam_inventory"}:
        return "库存"
    return str(platform or "-")


def _substrate_check_indicator_size(style: QStyle, widget: QWidget | None) -> QSize:
    iw = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, widget)
    ih = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight, None, widget)
    return QSize(
        max(iw, _SUBSTRATE_CHECK_INDICATOR_MIN),
        max(ih, _SUBSTRATE_CHECK_INDICATOR_MIN),
    )


def _row_uuid(item: dict) -> int | None:
    goods_name = str(item.get("goods_name", "") or "")
    tpl = get_template_from_goods_name(goods_name)
    if tpl is None:
        return None
    try:
        fv = float(item.get("float_value"))
    except (TypeError, ValueError):
        return None
    try:
        return SkinInstance(skin_template=tpl, float_value=fv).uuid
    except (ValueError, AssertionError):
        return None


def substrate_slot_lookup_key(
    *,
    name: object,
    float_value: object,
    platform: object,
) -> str | None:
    name_s = canonical_goods_name_for_lookup(str(name or ""))
    if not name_s:
        return None
    platform_s = str(platform or "").strip().lower()
    if not platform_s:
        return None
    try:
        float_s = format(float(np.float32(float(float_value))), ".18f")
    except (TypeError, ValueError):
        float_s = str(float_value or "").strip()
    if not float_s:
        return None
    return "||".join((name_s, float_s, platform_s))


def substrate_identity_key(*, name: object, float_value: object) -> str | None:
    """不含平台：与 ``substrate_slot_lookup_key`` 的前两段一致，用于跨平台导入等。"""
    name_s = canonical_goods_name_for_lookup(str(name or ""))
    if not name_s:
        return None
    try:
        float_s = format(float(np.float32(float(float_value))), ".18f")
    except (TypeError, ValueError):
        float_s = str(float_value or "").strip()
    if not float_s:
        return None
    return "||".join((name_s, float_s))


def substrate_row_lookup_key(item: dict) -> str | None:
    return substrate_slot_lookup_key(
        name=item.get("goods_name") or item.get("name"),
        float_value=item.get("float_value"),
        platform=item.get("platform"),
    )


class CheckCenterDelegate(QStyledItemDelegate):
    """委托 - 使复选框在单元格内居中，且仅复选框区域可点击"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        if (
            widget is not None
            and getattr(widget, "_hover_row", -1) >= 0
            and index.row() == widget._hover_row
        ):
            role = (
                QPalette.ColorRole.Mid
                if opt.state & QStyle.StateFlag.State_Selected
                else QPalette.ColorRole.Light
            )
            c = widget.palette().color(role)
            opt.palette.setColor(QPalette.ColorRole.Base, c)
            opt.palette.setColor(QPalette.ColorRole.AlternateBase, c)
        style = widget.style() if widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)
        if opt.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            if opt.checkState == Qt.CheckState.Unchecked:
                opt.state |= QStyle.StateFlag.State_Off
            elif opt.checkState == Qt.CheckState.PartiallyChecked:
                opt.state |= QStyle.StateFlag.State_NoChange
            else:
                opt.state |= QStyle.StateFlag.State_On
            rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, widget)
            min_sz = _substrate_check_indicator_size(style, widget)
            sz = QSize(
                max(rect.width(), min_sz.width()),
                max(rect.height(), min_sz.height()),
            )
            opt.rect = style.alignedRect(
                opt.direction, Qt.AlignmentFlag.AlignCenter, sz, opt.rect
            )
            opt.state &= ~QStyle.StateFlag.State_HasFocus
            style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, opt, painter, widget)

    def editorEvent(self, event, model, option, index):
        if not (model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        if value is None:
            return False
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        if event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonPress):
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, widget)
            min_sz = _substrate_check_indicator_size(style, widget)
            sz = QSize(
                max(rect.width(), min_sz.width()),
                max(rect.height(), min_sz.height()),
            )
            check_rect = style.alignedRect(
                opt.direction, Qt.AlignmentFlag.AlignCenter, sz, opt.rect
            )
            me = event
            if me.button() != Qt.MouseButton.LeftButton or not check_rect.contains(me.pos()):
                return False
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                return True
        elif event.type() == QEvent.Type.KeyPress:
            ke = event
            if ke.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
                return False
        else:
            return False
        state = value if isinstance(value, Qt.CheckState) else Qt.CheckState(int(value))
        new_state = Qt.CheckState.Unchecked if state == Qt.CheckState.Checked else Qt.CheckState.Checked
        return model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole)


class WholeRowHoverDelegate(QStyledItemDelegate):
    """整行 hover 背景（需表格设置 _hover_row 并对 viewport 安装事件过滤）。"""

    def __init__(self, table: QTableWidget):
        super().__init__(table)
        self._table = table

    def _apply_row_hover_palette(self, opt: QStyleOptionViewItem, index) -> None:
        t = self._table
        if getattr(t, "_hover_row", -1) >= 0 and index.row() == t._hover_row:
            role = (
                QPalette.ColorRole.Mid
                if opt.state & QStyle.StateFlag.State_Selected
                else QPalette.ColorRole.Light
            )
            c = t.palette().color(role)
            opt.palette.setColor(QPalette.ColorRole.Base, c)
            opt.palette.setColor(QPalette.ColorRole.AlternateBase, c)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        self._apply_row_hover_palette(opt, index)
        super().paint(painter, opt, index)


class SubstrateTableDelegate(WholeRowHoverDelegate):
    """底物选择表：整行 hover；勾选列用 CheckCenterDelegate 绘制复选框。"""

    def __init__(self, table: "AlchemyCollapsibleTableWidget"):
        super().__init__(table)
        self._check = CheckCenterDelegate(table)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        self._apply_row_hover_palette(opt, index)
        if index.column() in self._table._CHECK_COLUMNS:
            self._check.paint(painter, opt, index)
        else:
            QStyledItemDelegate.paint(self, painter, opt, index)

    def editorEvent(self, event, model, option, index):
        if index.column() in self._table._CHECK_COLUMNS:
            return self._check.editorEvent(event, model, option, index)
        return super().editorEvent(event, model, option, index)


class AlchemyTableWidget(QTableWidget):
    """炼金表格 - 支持 Ctrl+C 复制选中文本"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlternatingRowColors(False)
        self.viewport().setMouseTracking(True)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _copy_selection(self):
        items = self.selectedItems()
        if not items:
            return
        rows = sorted(set(item.row() for item in items))
        cols = sorted(set(item.column() for item in items))
        lines = []
        for row in rows:
            cells = []
            for col in cols:
                it = self.item(row, col)
                if it is None:
                    cells.append("")
                elif (
                    col in set(getattr(self, "_CHECK_COLUMNS", {getattr(self, "_CHECK_COL", 3)}))
                    and hasattr(it, "checkState")
                    and it.flags() & Qt.ItemFlag.ItemIsUserCheckable
                ):
                    cells.append("是" if it.checkState() == Qt.CheckState.Checked else "否")
                else:
                    cells.append(it.text())
            lines.append("\t".join(cells))
        text = "\n".join(lines)
        if text:
            QApplication.clipboard().setText(text)


class AlchemyRecipeRowHoverTableWidget(AlchemyTableWidget):
    """配方结果等表格：整行 hover，无勾选列特殊逻辑。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_row = -1
        self.viewport().installEventFilter(self)
        self.setItemDelegate(WholeRowHoverDelegate(self))

    def eventFilter(self, obj, event):
        if obj is self.viewport():
            et = event.type()
            if et == QEvent.Type.MouseMove:
                pos = event.position().toPoint()
                idx = self.indexAt(pos)
                row = idx.row() if idx.isValid() else -1
                if row != self._hover_row:
                    self._hover_row = row
                    self.viewport().update()
                return False
            if et == QEvent.Type.Leave:
                if self._hover_row != -1:
                    self._hover_row = -1
                    self.viewport().update()
                return False
        return super().eventFilter(obj, event)


class AlchemyCollapsibleTableWidget(AlchemyTableWidget):
    """底物数据选择表：点击行内任意列切换「是否参与计算」，两列复选框各自独立响应。"""

    _PRICE_COL = 3
    _MUST_COL = 5
    _CHECK_COL = 6
    _CHECK_COLUMNS = frozenset({_MUST_COL, _CHECK_COL})

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_row = -1
        self.viewport().installEventFilter(self)
        self.setItemDelegate(SubstrateTableDelegate(self))

    def _check_indicator_rect_for_row(self, row: int, col: int) -> QRect:
        index = self.model().index(row, col)
        r = self.visualRect(index)
        opt = QStyleOptionViewItem()
        opt.initFrom(self.viewport())
        opt.rect = r
        opt.features = QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        ci = self.item(row, col)
        if ci and ci.checkState() == Qt.CheckState.Checked:
            opt.state |= QStyle.StateFlag.State_On
        else:
            opt.state |= QStyle.StateFlag.State_Off
        st = self.style()
        vp = self.viewport()
        cb = st.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator,
            opt,
            vp,
        )
        min_sz = _substrate_check_indicator_size(st, vp)
        if cb.isValid() and cb.width() > 0:
            sz = QSize(
                max(cb.width(), min_sz.width()),
                max(cb.height(), min_sz.height()),
            )
            return st.alignedRect(
                Qt.LayoutDirection.LeftToRight,
                Qt.AlignmentFlag.AlignCenter,
                sz,
                r,
            )
        sz = min_sz
        return st.alignedRect(
            Qt.LayoutDirection.LeftToRight,
            Qt.AlignmentFlag.AlignCenter,
            sz,
            r,
        )

    def eventFilter(self, obj, event):
        if obj is self.viewport():
            et = event.type()
            if et == QEvent.Type.MouseMove:
                pos = event.position().toPoint()
                idx = self.indexAt(pos)
                row = idx.row() if idx.isValid() else -1
                if row != self._hover_row:
                    self._hover_row = row
                    self.viewport().update()
                return False
            if et == QEvent.Type.Leave:
                if self._hover_row != -1:
                    self._hover_row = -1
                    self.viewport().update()
                return False
            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.LeftButton:
                vp_pos = event.position().toPoint()
                idx = self.indexAt(vp_pos)
                if idx.isValid():
                    row = idx.row()
                    col = idx.column()
                    if col == self._PRICE_COL:
                        return False
                    calc_item = self.item(row, self._CHECK_COL)
                    if calc_item is not None and (
                        calc_item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                    ):
                        is_check_hit = False
                        if col in self._CHECK_COLUMNS:
                            cb_rect = self._check_indicator_rect_for_row(row, col)
                            is_check_hit = cb_rect.contains(vp_pos)
                        if is_check_hit:
                            pass
                        else:
                            new_state = (
                                Qt.CheckState.Unchecked
                                if calc_item.checkState() == Qt.CheckState.Checked
                                else Qt.CheckState.Checked
                            )
                            calc_item.setCheckState(new_state)
                return False
        return super().eventFilter(obj, event)


class NumericTableItem(QTableWidgetItem):
    """用于数值列排序：UserRole 存原始数值，DisplayRole 存显示文本"""

    def __lt__(self, other):
        a = self.data(Qt.ItemDataRole.UserRole)
        b = other.data(Qt.ItemDataRole.UserRole)
        if a is None:
            a = float("inf")
        if b is None:
            b = float("inf")
        return (a if isinstance(a, (int, float)) else float("inf")) < (
            b if isinstance(b, (int, float)) else float("inf")
        )


class CollapsibleGroup(QFrame):
    """可展开/收起的商品组 - 收起时仅显示 goods_name 长按钮，点击后向下展开表格"""

    def __init__(self, goods_name: str, items: list, parent=None, on_selection_changed=None):
        super().__init__(parent)
        self.setObjectName("alchemyGroup")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._expanded = False
        self._items = items
        self._goods_name = goods_name
        self._on_selection_changed = on_selection_changed
        self._slot_key_to_row: dict[str, int] = {}
        # Large imports used to eagerly build seven QTableWidgetItem objects for
        # every substrate, even though groups start collapsed.  Keep selection
        # state in a lightweight list and only materialise the table when the
        # user expands it.  This avoids multi-second UI stalls and large memory
        # spikes for inventories containing thousands of rows.
        self._table_populated = False
        self._row_states: list[list[bool]] = [[True, False] for _ in items]
        self._slot_key_to_item_index: dict[str, int] = {}
        for item_index, source_item in enumerate(items):
            slot_key = substrate_row_lookup_key(source_item)
            if slot_key and slot_key not in self._slot_key_to_item_index:
                self._slot_key_to_item_index[slot_key] = item_index

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

        title_with_tag = QWidget(self.header)
        title_with_tag.setObjectName("alchemyGroupTitleWithTag")
        title_tag_layout = QHBoxLayout(title_with_tag)
        title_tag_layout.setContentsMargins(0, 0, 0, 0)
        title_tag_layout.setSpacing(4)

        quality = get_quality_from_goods_name(goods_name)
        self.quality_label = QLabel(quality or "", title_with_tag)
        self.quality_label.setObjectName("alchemyQualityTag")
        self.quality_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if quality and quality in QUALITY_COLORS:
            bg, fg = QUALITY_COLORS[quality]
            self.quality_label.setStyleSheet(
                f"background: {bg}; color: {fg}; padding: 2px 8px; "
                f"border-radius: 4px; font-size: 11px; font-weight: 500;"
            )
        self.quality_label.setVisible(bool(quality))
        title_tag_layout.addWidget(self.quality_label, 0, Qt.AlignVCenter)

        self.title_label = QLabel(goods_name, title_with_tag)
        self.title_label.setObjectName("alchemyGroupTitle")
        self.title_label.setWordWrap(False)
        title_tag_layout.addWidget(self.title_label, 0, Qt.AlignVCenter)

        self.count_label = QLabel(f"×{len(items)}", title_with_tag)
        self.count_label.setObjectName("alchemyGroupCount")
        self.count_label.setToolTip(f"共 {len(items)} 条数据")
        title_tag_layout.addWidget(self.count_label, 0, Qt.AlignVCenter)

        title_tag_layout.addStretch(1)
        header_layout.addWidget(title_with_tag, 1, Qt.AlignVCenter)

        self.select_all_widget = QWidget(self.header)
        self.select_all_widget.setObjectName("alchemyGroupSelectAllWidget")
        self.select_all_widget.setCursor(Qt.PointingHandCursor)

        def on_select_all_clicked(event):
            if event.button() == Qt.LeftButton:
                self.select_all_check.toggle()

        self.select_all_widget.mousePressEvent = on_select_all_clicked
        select_all_layout = QHBoxLayout(self.select_all_widget)
        select_all_layout.setContentsMargins(0, 0, 0, 0)
        select_all_layout.setSpacing(6)
        self.select_all_label = QLabel("全选", self.select_all_widget)
        self.select_all_label.setObjectName("alchemyGroupSelectAll")
        self.select_all_check = QCheckBox(self.select_all_widget)
        self.select_all_check.setObjectName("alchemySubstratePickSelectAllCheck")
        self.select_all_check.setText("")
        self.select_all_check.setCheckState(Qt.CheckState.Checked)
        self.select_all_check.stateChanged.connect(self._on_select_all_changed)
        select_all_layout.addWidget(self.select_all_label, 0, Qt.AlignVCenter)
        select_all_layout.addWidget(self.select_all_check, 0, Qt.AlignVCenter)
        header_layout.addWidget(self.select_all_widget, 0, Qt.AlignVCenter)

        main_layout.addWidget(self.header)

        self.table_frame = QFrame(self)
        self.table_frame.setObjectName("alchemyTableFrame")
        self.table_frame.setVisible(False)
        table_layout = QVBoxLayout(self.table_frame)
        table_layout.setContentsMargins(16, 12, 16, 12)
        table_layout.setSpacing(0)

        self.table_widget = AlchemyCollapsibleTableWidget(self.table_frame)
        self.table_widget.setObjectName("alchemySubstratePickTable")
        self.table_widget.setColumnCount(7)
        self.table_widget.setHorizontalHeaderLabels(
            [
                "数据来源",
                "磨损等级",
                "磨损度",
                "价格（双击修改）",
                "武器箱/收藏品",
                "是否必须选择",
                "是否参与计算",
            ]
        )
        metrics = QFontMetrics(self.table_widget.font())
        self.table_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table_widget.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table_widget.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table_widget.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table_widget.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table_widget.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        self.table_widget.horizontalHeader().setSectionResizeMode(6, QHeaderView.Fixed)
        self.table_widget.setColumnWidth(0, metrics.horizontalAdvance("数据来源") + 28)
        self.table_widget.setColumnWidth(1, metrics.horizontalAdvance("战痕累累") + 34)
        self.table_widget.setColumnWidth(5, metrics.horizontalAdvance("是否必须选择") + 42)
        self.table_widget.setColumnWidth(6, metrics.horizontalAdvance("是否参与计算") + 42)
        self.table_widget.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.verticalHeader().setDefaultSectionSize(44)
        self.table_widget.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table_widget.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_widget.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table_widget.setMinimumHeight(400)
        self.table_widget.setSortingEnabled(True)
        self.table_widget.itemChanged.connect(self._on_table_item_changed)
        self.table_widget.horizontalHeader().sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        table_layout.addWidget(self.table_widget)
        main_layout.addWidget(self.table_frame)

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

    def _populate_table(self):
        if self._table_populated:
            return
        self.table_widget.blockSignals(True)
        self.table_widget.setSortingEnabled(False)
        self.table_widget.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            float_val = item.get("float_value")
            if float_val is not None:
                float_str = f"{float_val:.18f}" if isinstance(float_val, (int, float)) else str(float_val)
            else:
                float_str = "-"
            platform = _platform_display_name(item.get("platform", "-"))
            price = item.get("price")
            if isinstance(price, (int, float)):
                price_str = f"{price:.2f}"
            else:
                price_str = str(price) if price is not None else "-"

            align_center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
            platform_item = QTableWidgetItem(platform)
            platform_item.setFlags(
                platform_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            platform_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 0, platform_item)
            try:
                wear_level = (
                    SkinInstance.get_appearance(float(float_val))
                    if float_val is not None
                    else None
                )
            except (TypeError, ValueError):
                wear_level = None
            wear_level_item = QTableWidgetItem(wear_level or "未知")
            wear_level_item.setFlags(
                wear_level_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            wear_level_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 1, wear_level_item)
            float_item = NumericTableItem(float_str)
            float_item.setFlags(float_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            float_item.setData(Qt.ItemDataRole.UserRole, float_val)
            float_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 2, float_item)
            price_item = NumericTableItem(price_str)
            price_item.setData(Qt.ItemDataRole.UserRole, price if isinstance(price, (int, float)) else None)
            price_item.setData(Qt.ItemDataRole.UserRole + 1, item)
            price_item.setToolTip("双击可输入实际购入价格")
            price_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 3, price_item)
            wb_raw = item.get("weapon_box")
            if isinstance(wb_raw, str) and wb_raw.strip():
                wb_str = wb_raw.strip()
            elif wb_raw not in (None, "") and not isinstance(wb_raw, str):
                wb_str = str(wb_raw).strip()
            else:
                wb_str = ""
            if not wb_str:
                tpl = get_template_from_goods_name(str(item.get("goods_name", "")))
                if tpl and tpl.weapon_box_name:
                    wb_str = "、".join(tpl.weapon_box_name)
                else:
                    wb_str = "-"
            wb_item = QTableWidgetItem(wb_str)
            wb_item.setFlags(wb_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            wb_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 4, wb_item)
            must_item = QTableWidgetItem()
            must_item.setFlags(
                (must_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            must_item.setCheckState(Qt.CheckState.Unchecked)
            must_item.setTextAlignment(align_center)
            self.table_widget.setItem(row, 5, must_item)
            calc_item = QTableWidgetItem()
            calc_item.setFlags(
                (calc_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            calc_item.setCheckState(Qt.CheckState.Checked)
            calc_item.setTextAlignment(align_center)
            calc_item.setData(Qt.ItemDataRole.UserRole, item)
            calc_item.setData(Qt.ItemDataRole.UserRole + 1, _row_uuid(item))
            calc_item.setData(Qt.ItemDataRole.UserRole + 2, substrate_row_lookup_key(item))
            calc_item.setData(Qt.ItemDataRole.UserRole + 3, row)
            calc_checked, must_checked = self._row_states[row]
            must_item.setCheckState(
                Qt.CheckState.Checked if must_checked else Qt.CheckState.Unchecked
            )
            calc_item.setCheckState(
                Qt.CheckState.Checked if calc_checked else Qt.CheckState.Unchecked
            )
            self.table_widget.setItem(row, 6, calc_item)

        self.table_widget.setSortingEnabled(True)
        self.table_widget.horizontalHeader().setSortIndicator(2, Qt.SortOrder.AscendingOrder)
        self.table_widget.sortItems(2, Qt.SortOrder.AscendingOrder)
        self._table_populated = True
        self._rebuild_slot_key_row_index()
        self.table_widget.blockSignals(False)
        self._sync_select_all_from_model()

    def ensure_table_populated(self) -> None:
        """Build the expensive row widgets on demand."""
        self._populate_table()

    def _item_index_for_table_row(self, row: int) -> int | None:
        calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
        if calc_item is None:
            return None
        item_index = calc_item.data(Qt.ItemDataRole.UserRole + 3)
        if isinstance(item_index, int) and 0 <= item_index < len(self._items):
            return item_index
        return None

    def _sync_model_state_from_table_row(self, row: int) -> None:
        item_index = self._item_index_for_table_row(row)
        if item_index is None:
            return
        calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
        must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
        self._row_states[item_index] = [
            bool(calc_item and calc_item.checkState() == Qt.CheckState.Checked),
            bool(must_item and must_item.checkState() == Qt.CheckState.Checked),
        ]

    def _on_sort_indicator_changed(self, logical_index: int, order: Qt.SortOrder):
        if logical_index in self.table_widget._CHECK_COLUMNS:
            self.table_widget.horizontalHeader().blockSignals(True)
            self.table_widget.horizontalHeader().setSortIndicator(2, order)
            self.table_widget.sortItems(2, order)
            self.table_widget.horizontalHeader().blockSignals(False)
        self._rebuild_slot_key_row_index()

    def _rebuild_slot_key_row_index(self) -> None:
        mapping: dict[str, int] = {}
        for row in range(self.table_widget.rowCount()):
            calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
            if calc_item is None:
                continue
            slot_key = calc_item.data(Qt.ItemDataRole.UserRole + 2)
            if isinstance(slot_key, str) and slot_key and slot_key not in mapping:
                mapping[slot_key] = row
        self._slot_key_to_row = mapping
        logger.debug(
            "重建底物表槽位索引: goods=%s rows=%d indexed=%d",
            self._goods_name,
            self.table_widget.rowCount(),
            len(mapping),
        )

    def _on_select_all_changed(self, state):
        checked = state == Qt.CheckState.Checked.value
        for row_state in self._row_states:
            row_state[0] = checked
            if not checked:
                row_state[1] = False
        self.table_widget.blockSignals(True)
        for row in range(self.table_widget.rowCount()):
            calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
            must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
            if calc_item:
                calc_item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
            if not checked and must_item:
                must_item.setCheckState(Qt.CheckState.Unchecked)
        self.table_widget.blockSignals(False)
        if self._on_selection_changed:
            self._on_selection_changed()

    def _sync_select_all_from_table(self) -> None:
        """根据各行「是否参与计算」同步表头全选勾选框。"""
        checked_count = 0
        for row in range(self.table_widget.rowCount()):
            it = self.table_widget.item(row, self.table_widget._CHECK_COL)
            if it and it.checkState() == Qt.CheckState.Checked:
                checked_count += 1
        total = self.table_widget.rowCount()
        all_checked = total > 0 and checked_count == total
        self.select_all_check.blockSignals(True)
        self.select_all_check.setCheckState(Qt.CheckState.Checked if all_checked else Qt.CheckState.Unchecked)
        self.select_all_check.blockSignals(False)

    def _sync_select_all_from_model(self) -> None:
        total = len(self._row_states)
        all_checked = total > 0 and all(state[0] for state in self._row_states)
        self.select_all_check.blockSignals(True)
        self.select_all_check.setCheckState(
            Qt.CheckState.Checked if all_checked else Qt.CheckState.Unchecked
        )
        self.select_all_check.blockSignals(False)

    def _on_table_item_changed(self, item):
        if item.column() == self.table_widget._PRICE_COL:
            self._on_price_item_changed(item)
            return
        if item.column() not in self.table_widget._CHECK_COLUMNS:
            return
        if item.column() == self.table_widget._MUST_COL:
            calc_item = self.table_widget.item(item.row(), self.table_widget._CHECK_COL)
            if (
                item.checkState() == Qt.CheckState.Checked
                and calc_item is not None
                and calc_item.checkState() != Qt.CheckState.Checked
            ):
                self.table_widget.blockSignals(True)
                calc_item.setCheckState(Qt.CheckState.Checked)
                self.table_widget.blockSignals(False)
        elif item.column() == self.table_widget._CHECK_COL:
            must_item = self.table_widget.item(item.row(), self.table_widget._MUST_COL)
            if (
                item.checkState() != Qt.CheckState.Checked
                and must_item is not None
                and must_item.checkState() != Qt.CheckState.Unchecked
            ):
                self.table_widget.blockSignals(True)
                must_item.setCheckState(Qt.CheckState.Unchecked)
                self.table_widget.blockSignals(False)
        self._sync_model_state_from_table_row(item.row())
        self._sync_select_all_from_table()
        if self._on_selection_changed:
            self._on_selection_changed()

    def _on_price_item_changed(self, price_item: QTableWidgetItem) -> None:
        raw = price_item.text().strip().replace("¥", "").replace("￥", "").replace(",", "")
        source = price_item.data(Qt.ItemDataRole.UserRole + 1)
        previous = (
            source.get("price")
            if isinstance(source, dict)
            else price_item.data(Qt.ItemDataRole.UserRole)
        )
        try:
            value = float(raw)
            if not np.isfinite(value) or value < 0:
                raise ValueError
        except (TypeError, ValueError):
            QApplication.beep()
            self.table_widget.blockSignals(True)
            price_item.setText(
                f"{float(previous):.2f}"
                if isinstance(previous, (int, float))
                else "-"
            )
            self.table_widget.blockSignals(False)
            return

        if isinstance(source, dict):
            source["price"] = value
            slot_key = substrate_row_lookup_key(source)
            for original in self._items:
                if substrate_row_lookup_key(original) == slot_key:
                    original["price"] = value
                    break
            calc_item = self.table_widget.item(
                price_item.row(),
                self.table_widget._CHECK_COL,
            )
            if calc_item is not None:
                calc_data = calc_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(calc_data, dict):
                    calc_data["price"] = value
                    calc_item.setData(Qt.ItemDataRole.UserRole, calc_data)
        self.table_widget.blockSignals(True)
        price_item.setData(Qt.ItemDataRole.UserRole, value)
        price_item.setText(f"{value:.2f}")
        self.table_widget.blockSignals(False)
        if self._on_selection_changed:
            self._on_selection_changed()

    def uncheck_rows_where(self, should_uncheck: Callable[[dict], bool]) -> int:
        """
        对满足条件的行取消「是否参与计算」勾选（仅原先为勾选状态的行计入返回值）。
        """
        n = 0
        if not self._table_populated:
            for item_index, data in enumerate(self._items):
                if self._row_states[item_index][0] and should_uncheck(data):
                    self._row_states[item_index] = [False, False]
                    n += 1
            self._sync_select_all_from_model()
            return n
        self.table_widget.blockSignals(True)
        self.select_all_check.blockSignals(True)
        for row in range(self.table_widget.rowCount()):
            calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
            must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
            if not calc_item or calc_item.checkState() != Qt.CheckState.Checked:
                continue
            data = calc_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            if should_uncheck(data):
                calc_item.setCheckState(Qt.CheckState.Unchecked)
                if must_item:
                    must_item.setCheckState(Qt.CheckState.Unchecked)
                self._sync_model_state_from_table_row(row)
                n += 1
        self.table_widget.blockSignals(False)
        self._sync_select_all_from_table()
        self.select_all_check.blockSignals(False)
        return n

    def get_all_slot_keys(self) -> set[str]:
        return {
            k for k in self._slot_key_to_item_index.keys() if isinstance(k, str) and k
        }

    def find_row_by_slot_key(self, slot_key: str) -> int | None:
        if not slot_key:
            return None
        row = self._slot_key_to_row.get(slot_key)
        if isinstance(row, int):
            return row
        return None

    def get_row_state_by_slot_key(self, slot_key: str) -> tuple[bool, bool] | None:
        item_index = self._slot_key_to_item_index.get(slot_key)
        if item_index is None:
            return None
        calc_checked, must_checked = self._row_states[item_index]
        return (calc_checked, must_checked)

    def set_row_state_by_slot_key(
        self,
        slot_key: str,
        *,
        calc_checked: bool,
        must_checked: bool,
    ) -> bool:
        item_index = self._slot_key_to_item_index.get(slot_key)
        if item_index is None:
            return False
        self._row_states[item_index] = [bool(calc_checked), bool(must_checked)]
        if not self._table_populated:
            self._sync_select_all_from_model()
            if self._on_selection_changed:
                self._on_selection_changed()
            return True
        row = self.find_row_by_slot_key(slot_key)
        if row is None:
            return False
        calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
        if calc_item is None:
            return False
        must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
        self.table_widget.blockSignals(True)
        self.select_all_check.blockSignals(True)
        if must_item is not None:
            must_item.setCheckState(
                Qt.CheckState.Checked if must_checked else Qt.CheckState.Unchecked
            )
        calc_item.setCheckState(
            Qt.CheckState.Checked if calc_checked else Qt.CheckState.Unchecked
        )
        self.table_widget.blockSignals(False)
        self._sync_select_all_from_table()
        self.select_all_check.blockSignals(False)
        if self._on_selection_changed:
            self._on_selection_changed()
        return True

    def set_row_calc_must_at_row(
        self, row: int, *, calc_checked: bool, must_checked: bool
    ) -> bool:
        """按行号写回「参与计算 / 必选」，不依赖 ``_slot_key_to_row``（供配方导入等）。
        若目标状态与当前一致则不改写并返回 False，避免误报「已同步」但界面无变化。"""
        if row < 0 or row >= self.table_widget.rowCount():
            return False
        calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
        if calc_item is None:
            return False
        must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
        cur_calc = calc_item.checkState() == Qt.CheckState.Checked
        cur_must = bool(
            must_item and must_item.checkState() == Qt.CheckState.Checked
        )
        if cur_calc == calc_checked and cur_must == must_checked:
            return False
        self.table_widget.blockSignals(True)
        self.select_all_check.blockSignals(True)
        if must_item is not None:
            must_item.setCheckState(
                Qt.CheckState.Checked if must_checked else Qt.CheckState.Unchecked
            )
        calc_item.setCheckState(
            Qt.CheckState.Checked if calc_checked else Qt.CheckState.Unchecked
        )
        self._sync_model_state_from_table_row(row)
        self.table_widget.blockSignals(False)
        self._sync_select_all_from_table()
        self.select_all_check.blockSignals(False)
        self.table_widget.viewport().update()
        if self._on_selection_changed:
            self._on_selection_changed()
        return True

    def get_selected_items(self) -> list:
        if not self._table_populated:
            result = []
            for item, (calc_checked, must_checked) in zip(
                self._items, self._row_states
            ):
                if calc_checked:
                    row_data = dict(item)
                    row_data["must_select"] = bool(must_checked)
                    result.append(row_data)
            return result
        result = []
        for row in range(self.table_widget.rowCount()):
            calc_item = self.table_widget.item(row, self.table_widget._CHECK_COL)
            if calc_item and calc_item.checkState() == Qt.CheckState.Checked:
                data = calc_item.data(Qt.ItemDataRole.UserRole)
                if data:
                    must_item = self.table_widget.item(row, self.table_widget._MUST_COL)
                    row_data = dict(data)
                    row_data["must_select"] = bool(
                        must_item and must_item.checkState() == Qt.CheckState.Checked
                    )
                    result.append(row_data)
        return result

    def _on_header_clicked(self, event):
        if event.button() == Qt.LeftButton:
            child = self.header.childAt(event.pos())
            if child and (child == self.select_all_widget or self.select_all_widget.isAncestorOf(child)):
                return
            self.toggle()

    def toggle(self):
        self._expanded = not self._expanded
        if self._expanded:
            self.ensure_table_populated()
        self._update_disclosure_arrow()
        self.table_frame.setVisible(self._expanded)
        self.updateGeometry()
