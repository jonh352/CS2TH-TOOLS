"""采集预设：用户自定义材料列表与磨损区间，可导入材料采集页。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import CONTENT_PAGE_LAYOUT_MARGINS
from core.alchemy_quality import get_template_from_goods_name
from core.collection_presets import (
    delete_collection_preset,
    format_preset_saved_at,
    list_collection_presets,
    load_collection_preset,
    rename_collection_preset,
    save_collection_preset,
)
from core.data_utils import QUALITY_COLORS
from ui.dialogs.wide_text_input_dialog import get_wide_text_input
from ui.feedback import ask_confirmation
from ui.widgets.skin_search_field import SkinSearchField
from ui.widgets.toast import show_toast
from ui.widgets.wear_interval_bar import WearRangeSelector


class _PresetTitleLabel(QLabel):
    """Detail title; double-click renames the scheme."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _quality_chip(quality: str) -> QLabel:
    label = QLabel(quality or "未知")
    label.setObjectName("collectionPresetQualityChip")
    bg, fg = QUALITY_COLORS.get(quality or "", ("#64748b", "#ffffff"))
    label.setStyleSheet(
        f"QLabel#collectionPresetQualityChip {{"
        f" background-color: {bg}; color: {fg};"
        f" border-radius: 3px; padding: 2px 6px; font-weight: 600; }}"
    )
    return label


class _PresetItemCard(QFrame):
    removed = Signal(object)

    def __init__(
        self,
        *,
        name: str,
        min_wear: float,
        max_wear: float,
        editable: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collectionPresetItemCard")
        self.name = name
        template = get_template_from_goods_name(name)
        quality = str(getattr(template, "quality", "") or "")
        bound_low = float(getattr(template, "min_float", 0.0) or 0.0)
        bound_high = float(getattr(template, "max_float", 1.0) or 1.0)
        if bound_high <= bound_low:
            bound_low, bound_high = 0.0, 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(name)
        title.setObjectName("collectionPresetItemTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        if quality:
            head.addWidget(_quality_chip(quality), 0, Qt.AlignmentFlag.AlignTop)
        if editable:
            remove_btn = QPushButton("移除")
            remove_btn.setObjectName("marketOpenButton")
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.clicked.connect(lambda: self.removed.emit(self))
            head.addWidget(remove_btn, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self.range_label = QLabel()
        self.range_label.setObjectName("muted")
        layout.addWidget(self.range_label)

        self.selector = WearRangeSelector()
        low = max(bound_low, min(bound_high, float(min_wear)))
        high = max(bound_low, min(bound_high, float(max_wear)))
        if high < low:
            low, high = high, low
        self.selector.set_wear_bounds(
            bound_low,
            bound_high,
            selected_min=low,
            selected_max=high,
        )
        self.selector.setEnabled(editable)
        self.selector.rangeChanged.connect(self._on_range_changed)
        layout.addWidget(self.selector)
        self._on_range_changed(low, high)

    def _on_range_changed(self, low: float, high: float) -> None:
        self.range_label.setText(f"已选择磨损 {low:g} ～ {high:g}")

    def to_item(self) -> dict[str, Any]:
        low, high = self.selector.selected_range()
        return {"name": self.name, "min_wear": float(low), "max_wear": float(high)}


class CollectionPresetPage(QWidget):
    """Sidebar of named schemes + detail / editor for wear ranges."""

    import_to_collection_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collectionPresetPage")
        self._presets: list[dict[str, Any]] = []
        self._current_id = ""
        self._editing = False
        self._item_cards: list[_PresetItemCard] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        root.setSpacing(14)

        # —— Left: scheme list ——
        left = QFrame()
        left.setObjectName("collectionPresetSidebar")
        left.setFixedWidth(240)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(12, 12, 12, 12)
        left_lay.setSpacing(10)

        side_title = QLabel("采集方案")
        side_title.setObjectName("pageTitle")
        left_lay.addWidget(side_title)

        self.scheme_list = QListWidget()
        self.scheme_list.setObjectName("collectionPresetList")
        self.scheme_list.setToolTip("双击方案名可重命名")
        self.scheme_list.currentItemChanged.connect(self._on_scheme_selected)
        self.scheme_list.itemDoubleClicked.connect(self._rename_scheme_from_list)
        left_lay.addWidget(self.scheme_list, 1)

        self.new_btn = QPushButton("新建采集方案")
        self.new_btn.setObjectName("primaryButton")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self._start_create)
        left_lay.addWidget(self.new_btn)
        root.addWidget(left)

        # —— Right: detail / editor ——
        right = QFrame()
        right.setObjectName("panel")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(18, 16, 18, 16)
        right_lay.setSpacing(12)

        self.stack = QStackedWidget()
        right_lay.addWidget(self.stack, 1)
        root.addWidget(right, 1)

        empty = QWidget()
        empty_lay = QVBoxLayout(empty)
        empty_lay.addStretch(1)
        empty_hint = QLabel("选择左侧方案，或新建采集方案")
        empty_hint.setObjectName("muted")
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(empty_hint)
        empty_lay.addStretch(1)
        self.stack.addWidget(empty)

        detail = QWidget()
        detail_lay = QVBoxLayout(detail)
        detail_lay.setContentsMargins(0, 0, 0, 0)
        detail_lay.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title_label = _PresetTitleLabel("方案")
        self.title_label.setObjectName("pageTitle")
        self.title_label.setToolTip("双击可重命名方案")
        self.title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_label.double_clicked.connect(self._rename_current_scheme)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        titles.addWidget(self.title_label)
        titles.addWidget(self.meta_label)
        header.addLayout(titles, 1)

        self.import_btn = QPushButton("导入采集")
        self.import_btn.setObjectName("primaryButton")
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.clicked.connect(self._import_to_collection)
        self.edit_btn = QPushButton("修改方案")
        self.edit_btn.setObjectName("marketOpenButton")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.clicked.connect(self._start_edit)
        self.delete_btn = QPushButton("删除方案")
        self.delete_btn.setObjectName("dangerButton")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self._delete_current)
        self.save_btn = QPushButton("保存方案")
        self.save_btn.setObjectName("primaryButton")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self._save_editor)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("marketOpenButton")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._cancel_editor)
        header.addWidget(self.import_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.edit_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.save_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignTop)
        detail_lay.addLayout(header)

        self.search_row = QWidget()
        search_lay = QHBoxLayout(self.search_row)
        search_lay.setContentsMargins(0, 0, 0, 0)
        search_lay.setSpacing(8)
        search_hint = QLabel("添加饰品")
        search_hint.setObjectName("muted")
        self.skin_search = SkinSearchField()
        self.skin_search.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.skin_search.skin_resolved.connect(self._on_skin_picked)
        self.skin_search.weapon_box_guns_selected.connect(self._on_weapon_box_picked)
        search_lay.addWidget(search_hint)
        search_lay.addWidget(self.skin_search, 1)
        detail_lay.addWidget(self.search_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.items_host = QWidget()
        self.items_layout = QVBoxLayout(self.items_host)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setSpacing(10)
        self.items_layout.addStretch(1)
        scroll.setWidget(self.items_host)
        detail_lay.addWidget(scroll, 1)

        self.stack.addWidget(detail)
        self._detail_page = detail

        self.refresh_from_disk()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._editing:
            self.refresh_from_disk(keep_selection=True)

    def refresh_from_disk(self, *, keep_selection: bool = False) -> None:
        selected = self._current_id if keep_selection else ""
        self._presets = list_collection_presets()
        self.scheme_list.blockSignals(True)
        self.scheme_list.clear()
        select_row = -1
        for index, preset in enumerate(self._presets):
            count = len(preset.get("items") or [])
            title = str(preset.get("title") or "未命名方案")
            item = QListWidgetItem(f"{title} ({count})")
            item.setData(Qt.ItemDataRole.UserRole, str(preset.get("id") or ""))
            self.scheme_list.addItem(item)
            if selected and str(preset.get("id") or "") == selected:
                select_row = index
        self.scheme_list.blockSignals(False)
        if select_row >= 0:
            self.scheme_list.setCurrentRow(select_row)
        elif self._presets and not self._editing:
            self.scheme_list.setCurrentRow(0)
        elif not self._presets and not self._editing:
            self._current_id = ""
            self.stack.setCurrentIndex(0)

    def _on_scheme_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if self._editing:
            return
        if current is None:
            self.stack.setCurrentIndex(0)
            self._current_id = ""
            return
        preset_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        data = load_collection_preset(preset_id)
        if data is None:
            show_toast(self, "方案不存在或已损坏", style="warning")
            self.refresh_from_disk()
            return
        self._show_preset(data, editing=False)

    def _show_preset(self, data: dict[str, Any], *, editing: bool) -> None:
        self._editing = editing
        self._current_id = str(data.get("id") or "")
        self.title_label.setText(str(data.get("title") or "未命名方案"))
        count = len(data.get("items") or [])
        saved = format_preset_saved_at(str(data.get("saved_at") or ""))
        if editing:
            self.meta_label.setText(f"编辑中 · {count} 种饰品")
        else:
            self.meta_label.setText(f"保存时间：{saved} · {count} 种饰品")

        self.import_btn.setVisible(not editing)
        self.edit_btn.setVisible(not editing)
        self.delete_btn.setVisible(not editing)
        self.save_btn.setVisible(editing)
        self.cancel_btn.setVisible(editing)
        self.search_row.setVisible(editing)
        self.scheme_list.setEnabled(not editing)
        self.new_btn.setEnabled(not editing)

        self._clear_item_cards()
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            self._add_item_card(
                name=str(item.get("name") or ""),
                min_wear=float(item.get("min_wear") or 0),
                max_wear=float(item.get("max_wear") or 1),
                editable=editing,
            )
        self.stack.setCurrentWidget(self._detail_page)

    def _clear_item_cards(self) -> None:
        while self.items_layout.count() > 1:
            item = self.items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._item_cards.clear()

    def _add_item_card(
        self,
        *,
        name: str,
        min_wear: float,
        max_wear: float,
        editable: bool,
    ) -> None:
        name = str(name or "").strip()
        if not name:
            return
        card = _PresetItemCard(
            name=name,
            min_wear=min_wear,
            max_wear=max_wear,
            editable=editable,
            parent=self.items_host,
        )
        card.removed.connect(self._remove_card)
        self.items_layout.insertWidget(self.items_layout.count() - 1, card)
        self._item_cards.append(card)

    def _remove_card(self, card: object) -> None:
        if not isinstance(card, _PresetItemCard):
            return
        if card in self._item_cards:
            self._item_cards.remove(card)
        self.items_layout.removeWidget(card)
        card.deleteLater()
        self.meta_label.setText(f"编辑中 · {len(self._item_cards)} 种饰品")

    def _append_skin_name(self, name: str) -> bool:
        """Add one skin by display name. Returns True if a new card was created."""
        name = str(name or "").strip()
        if not name:
            return False
        if any(card.name.casefold() == name.casefold() for card in self._item_cards):
            return False
        template = get_template_from_goods_name(name)
        if template is not None:
            low = float(getattr(template, "min_float", 0.0) or 0.0)
            high = float(getattr(template, "max_float", 1.0) or 1.0)
        else:
            low, high = 0.0, 1.0
        self._add_item_card(name=name, min_wear=low, max_wear=high, editable=True)
        return True

    def _on_skin_picked(self, template: object) -> None:
        if not self._editing or template is None:
            return
        weapon = str(getattr(template, "weapon_name", "") or "").strip()
        skin = str(getattr(template, "skin_name", "") or "").strip()
        name = f"{weapon} | {skin}" if skin else weapon
        name = name.strip()
        if not name:
            return
        if not self._append_skin_name(name):
            show_toast(self, "方案中已有该饰品", style="warning")
            return
        self.meta_label.setText(f"编辑中 · {len(self._item_cards)} 种饰品")
        self.skin_search.clear_for_next_entry()

    def _on_weapon_box_picked(self, names: object) -> None:
        """Add every skin from a weapon-box + quality candidate row."""
        if not self._editing or not isinstance(names, list):
            return
        added = 0
        skipped = 0
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                continue
            if self._append_skin_name(name):
                added += 1
            else:
                skipped += 1
        self.meta_label.setText(f"编辑中 · {len(self._item_cards)} 种饰品")
        if added <= 0:
            show_toast(
                self,
                "这些饰品已在方案中" if skipped else "未找到可添加的饰品",
                style="warning",
            )
            return
        msg = f"已添加 {added} 种饰品"
        if skipped:
            msg += f"（跳过已有 {skipped}）"
        show_toast(self, msg, style="success")

    def _collect_editor_items(self) -> list[dict[str, Any]]:
        return [card.to_item() for card in self._item_cards]

    def _start_create(self) -> None:
        title, ok = get_wide_text_input(
            self,
            title="新建采集方案",
            label="方案名称：",
            value="未命名方案",
        )
        if not ok:
            return
        title = str(title or "").strip() or "未命名方案"
        self._show_preset(
            {
                "id": "",
                "title": title,
                "saved_at": "",
                "items": [],
            },
            editing=True,
        )

    def _start_edit(self) -> None:
        if not self._current_id:
            return
        data = load_collection_preset(self._current_id)
        if data is None:
            show_toast(self, "方案不存在", style="warning")
            self.refresh_from_disk()
            return
        self._show_preset(data, editing=True)

    def _rename_scheme_from_list(self, item: QListWidgetItem) -> None:
        if self._editing or item is None:
            return
        preset_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not preset_id:
            return
        if preset_id != self._current_id:
            self.scheme_list.setCurrentItem(item)
        self._rename_current_scheme(preset_id=preset_id)

    def _rename_current_scheme(self, *, preset_id: str | None = None) -> None:
        target_id = str(preset_id or self._current_id or "").strip()
        if not target_id or self._editing:
            return
        data = load_collection_preset(target_id)
        if data is None:
            show_toast(self, "方案不存在", style="warning")
            self.refresh_from_disk()
            return
        title, ok = get_wide_text_input(
            self,
            title="重命名方案",
            label="方案名称：",
            value=str(data.get("title") or ""),
        )
        if not ok:
            return
        new_title = str(title or "").strip() or str(data.get("title") or "未命名方案")
        if new_title == str(data.get("title") or "").strip():
            return
        saved = rename_collection_preset(target_id, new_title)
        if saved is None:
            show_toast(self, "重命名失败", style="warning")
            return
        self._current_id = str(saved.get("id") or target_id)
        show_toast(self, "已重命名方案", style="success")
        self.refresh_from_disk(keep_selection=True)
        loaded = load_collection_preset(self._current_id)
        if loaded is not None:
            self._show_preset(loaded, editing=False)

    def _cancel_editor(self) -> None:
        self._editing = False
        self.scheme_list.setEnabled(True)
        self.new_btn.setEnabled(True)
        if self._current_id:
            data = load_collection_preset(self._current_id)
            if data is not None:
                self._show_preset(data, editing=False)
                self.refresh_from_disk(keep_selection=True)
                return
        self.refresh_from_disk()
        self.stack.setCurrentIndex(0)

    def _save_editor(self) -> None:
        items = self._collect_editor_items()
        if not items:
            show_toast(self, "请至少添加一种饰品", style="warning")
            return
        title = self.title_label.text().strip() or "未命名方案"
        saved = save_collection_preset(
            title=title,
            items=items,
            preset_id=self._current_id or None,
        )
        self._editing = False
        self._current_id = str(saved.get("id") or "")
        show_toast(self, "采集方案已保存", style="success")
        self.refresh_from_disk(keep_selection=True)
        data = load_collection_preset(self._current_id)
        if data is not None:
            self._show_preset(data, editing=False)

    def _delete_current(self) -> None:
        if not self._current_id:
            return
        title = self.title_label.text().strip() or "该方案"
        if not ask_confirmation(
            self,
            "删除采集方案",
            f"确定删除「{title}」？此操作不可恢复。",
        ):
            return
        if delete_collection_preset(self._current_id):
            show_toast(self, "已删除方案", style="success")
        else:
            show_toast(self, "删除失败", style="warning")
        self._current_id = ""
        self._editing = False
        self.refresh_from_disk()
        self.stack.setCurrentIndex(0)

    def _import_to_collection(self) -> None:
        if not self._current_id:
            return
        data = load_collection_preset(self._current_id)
        if data is None:
            show_toast(self, "方案不存在", style="warning")
            return
        items = list(data.get("items") or [])
        if not items:
            show_toast(self, "方案中没有饰品", style="warning")
            return
        self.import_to_collection_requested.emit(
            {
                "title": str(data.get("title") or "采集预设"),
                "items": items,
            }
        )
