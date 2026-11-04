"""采购管理页：按日期管理配方、归档与账号维度炼金统计。"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen,
    QPixmap, QShowEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDateEdit, QFrame, QGridLayout, QHBoxLayout,
    QDialog, QInputDialog, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from config import CONTENT_PAGE_LAYOUT_MARGINS, RECIPE_ICON_PATH
from core.inventory_steam_accounts import (
    combo_display_name_for_profile, get_active_profile_id, list_profile_entries,
    load_steam_account_config_dict,
)
from core.alchemy_quality import canonical_steam_market_hash_name, get_template_from_goods_name, resolve_inventory_skin_template
from core.inventory_icons import resolve_inventory_item_icon_path, weapon_image_path_from_skin_template
from core.purchase_batches import (
    delete_tradeup_completed_batches, list_purchase_batches,
    purchase_batch_summary, repair_purchase_account_links,
    update_purchase_batch_account, load_purchase_batch, purchase_recipe_section,
    delete_purchase_batch_recipe, move_purchase_batch_recipe, daily_purchase_batch,
    reconcile_all_purchase_records_for_profile,
    toggle_all_purchase_batch_materials_ordered,
    purchase_batch_recipe_alchemy_status_text,
)
from core.purchase_tracking import load_profile_inventory_items
from core.steam_tradeup_history import list_steam_tradeup_records
from core.eco_furnace import purchase_recipe_eco_readiness, purchase_recipe_for_furnace
from core.eco_publication import recipe_publication, record_recipe_publication
from ui.dialogs.edit_purchase_recipe_dialog import EditPurchaseRecipeDialog
from ui.feedback import ask_confirmation
from ui.icons import load_svg_icon, expand_section_triangle_icon
from ui.widgets.purchase_batch_card import PurchaseBatchCard, purchase_recipe_matches_item_search, _planned_recipe_cost, _FILTER_TO_STATUS
from ui.widgets.eco_recipe_card import EcoRecipeCard
from ui.widgets.toast import show_toast
from ui.workers.steam_history import SteamHistorySyncWorker


_SECTIONS = (
    ("purchasing", "采购管理"),
    ("tradeup_completed", "已炼金"),
    ("history", "汰换记录与统计"),
    ("eco", "ECO合炉"),
)


def _parse_local_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is not None else parsed


def _money(value: object) -> str:
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return "价格待刷新"


def _matches_alchemy_filter(entry, choice, now=None):
    if choice == "all":
        return True
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    ready = purchase_batch_recipe_alchemy_status_text(entry, now=now) == "可炼金"
    if choice == "ready":
        return ready
    if choice == "blocked":
        return not ready
    unlock = _parse_local_datetime(entry.get("alchemy_ready_at"))
    return not ready and bool(unlock and unlock > now and unlock.astimezone(now.tzinfo).date() == now.date()) and all(row.get("status") == "received" for row in entry.get("materials") or [])


class _HistoryProductCard(QFrame):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("查看汰换产物详情")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _ProfitTrendChart(QWidget):
    """Small theme-aware daily P/L chart used by the history dashboard."""

    def __init__(self, points: list[tuple[date, float]], parent=None) -> None:
        super().__init__(parent)
        self._points = points
        self.setObjectName("tradeupHistoryTrendChart")
        self.setMinimumHeight(210)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(58, 14, -18, -34)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        muted = self.palette().color(QPalette.ColorRole.PlaceholderText)
        grid_color = self.palette().color(QPalette.ColorRole.Midlight)
        if not self._points:
            painter.setPen(muted)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "当前区间暂无收益数据")
            return

        values = [value for _day, value in self._points]
        low = min(0.0, min(values))
        high = max(0.0, max(values))
        if abs(high - low) < 0.01:
            high, low = max(1.0, high), min(-1.0, low)
        padding = max(0.5, (high - low) * 0.08)
        high += padding
        low -= padding

        painter.setFont(QFont(painter.font().family(), 9))
        for step in range(5):
            ratio = step / 4
            y = rect.top() + ratio * rect.height()
            value = high - ratio * (high - low)
            painter.setPen(QPen(grid_color, 1))
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
            painter.setPen(muted)
            painter.drawText(0, int(y) - 9, 52, 18, Qt.AlignmentFlag.AlignRight, f"¥{value:.0f}")

        count = len(self._points)
        def point_xy(index: int, value: float) -> tuple[float, float]:
            x = rect.left() if count == 1 else rect.left() + index * rect.width() / (count - 1)
            y = rect.top() + (high - value) * rect.height() / (high - low)
            return x, y

        zero_y = rect.top() + (high * rect.height() / (high - low))
        painter.setPen(QPen(QColor("#94a3b8"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(rect.left(), int(zero_y), rect.right(), int(zero_y))
        path = QPainterPath()
        for index, (_day, value) in enumerate(self._points):
            x, y = point_xy(index, value)
            path.moveTo(x, y) if index == 0 else path.lineTo(x, y)
        painter.setPen(QPen(QColor("#f97316"), 3))
        painter.drawPath(path)
        painter.setBrush(QColor("#f97316"))
        for index, (_day, value) in enumerate(self._points):
            x, y = point_xy(index, value)
            painter.drawEllipse(int(x) - 3, int(y) - 3, 6, 6)

        label_indexes = sorted({
            round(i * (count - 1) / min(6, max(1, count - 1)))
            for i in range(min(7, count))
        })
        painter.setPen(muted)
        for index in label_indexes:
            x, _y = point_xy(index, values[index])
            label = self._points[index][0].strftime("%m-%d")
            painter.drawText(
                int(x) - 30, rect.bottom() + 8, 60, 20,
                Qt.AlignmentFlag.AlignHCenter, label,
            )


class PurchaseManagePage(QWidget):
    simulate_tradeup_requested = Signal(object)
    import_to_collection_requested = Signal(object)
    navigation_route_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("purchaseManagePage")
        self._current_section = "purchasing"
        self._purchase_batch_card_states: dict[str, dict[str, object]] = {}
        self._purchase_day_expanded: dict[tuple[str, str], bool] = {}
        self._purchase_batch_scroll_value = 0
        self._history_worker: SteamHistorySyncWorker | None = None
        self._history_trend_days = 7

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        main_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self._page_title_icon_label = QLabel(self)
        self._page_title_icon_label.setObjectName("contentPageTitleIcon")
        self._page_title_icon_label.setFixedSize(28, 28)
        self._page_title_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_row.addWidget(self._page_title_icon_label)
        title = QLabel("采购管理")
        title.setObjectName("alchemyPageTitle")
        font = QFont()
        font.setPointSize(18)
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        title_row.addWidget(title)
        self._title_count_label = QLabel("共0炉 · 0件材料")
        self._title_count_label.setObjectName("recipePageTitleCount")
        title_row.addWidget(self._title_count_label)
        title_row.addStretch(1)
        main_layout.addLayout(title_row)

        section_bar = QWidget(self)
        section_bar.setObjectName("purchaseManageSectionBar")
        section_row = QHBoxLayout(section_bar)
        section_row.setContentsMargins(0, 0, 0, 0)
        section_row.setSpacing(8)
        self._section_group = QButtonGroup(self)
        self._section_group.setExclusive(True)
        self._section_buttons: dict[str, QPushButton] = {}
        for key, label in _SECTIONS:
            button = QPushButton(label, section_bar)
            button.setObjectName("purchaseManageSectionButton")
            button.setCheckable(True)
            button.setChecked(key == self._current_section)
            button.setProperty("active", key == self._current_section)
            button.clicked.connect(lambda _checked=False, section=key: self._show_section(section))
            self._section_group.addButton(button)
            self._section_buttons[key] = button
            section_row.addWidget(button)
        section_row.addStretch(1)
        main_layout.addWidget(section_bar)

        self._toolbar = QWidget()
        self._toolbar.setObjectName("recipeManageToolbar")
        toolbar = QHBoxLayout(self._toolbar)
        toolbar.setContentsMargins(12, 8, 12, 8)
        toolbar.setSpacing(8)
        self._toolbar_label = QLabel()
        self._toolbar_label.setObjectName("recipeLocationLabel")
        toolbar.addWidget(self._toolbar_label)
        toolbar.addStretch(1)
        self._item_search_edit = QLineEdit(self._toolbar)
        self._item_search_edit.setPlaceholderText("搜索饰品名称…")
        self._item_search_edit.setToolTip("搜索材料原名或替代饰品名，显示匹配配方的全部材料")
        self._item_search_edit.setClearButtonEnabled(True)
        self._item_search_edit.setMinimumWidth(180)
        self._item_search_edit.setMaximumWidth(300)
        self._item_search_timer = QTimer(self)
        self._item_search_timer.setSingleShot(True)
        self._item_search_timer.setInterval(200)
        self._item_search_timer.timeout.connect(self._rebuild_purchase_batch_rows)
        self._item_search_edit.textChanged.connect(lambda _text: self._item_search_timer.start())
        toolbar.addWidget(self._item_search_edit)
        self._account_combo = QComboBox(self._toolbar)
        self._account_combo.setMinimumWidth(160)
        self._account_combo.currentIndexChanged.connect(self._on_account_filter_changed)
        toolbar.addWidget(self._account_combo)
        self._material_status_combo = QComboBox(self._toolbar)
        self._material_status_combo.setObjectName("purchaseGlobalMaterialFilter")
        self._material_status_combo.addItems(_FILTER_TO_STATUS)
        self._material_status_combo.setMinimumWidth(130)
        self._material_status_combo.currentTextChanged.connect(lambda _text: self._rebuild_purchase_batch_rows())
        toolbar.addWidget(self._material_status_combo)
        self._alchemy_filter_combo = QComboBox(self._toolbar)
        self._alchemy_filter_combo.setObjectName("purchaseGlobalAlchemyFilter")
        for label, value in (("全部配方", "all"), ("可炼金", "ready"), ("今日解锁", "today"), ("不可炼金", "blocked")):
            self._alchemy_filter_combo.addItem(label, value)
        self._alchemy_filter_combo.setMinimumWidth(110)
        self._alchemy_filter_combo.setToolTip("按已保存的炼金时间筛选；今日解锁指今天稍后解锁，尚未可炼金。不可炼金包含今日稍后解锁的配方。")
        self._alchemy_filter_combo.currentIndexChanged.connect(lambda _index: self._rebuild_purchase_batch_rows())
        toolbar.addWidget(self._alchemy_filter_combo)
        self._refresh_ready_btn = QPushButton("刷新", self._toolbar)
        self._refresh_ready_btn.setObjectName("alchemySelectFileBtn")
        self._refresh_ready_btn.clicked.connect(self._on_refresh_clicked)
        toolbar.addWidget(self._refresh_ready_btn)
        self._start_date = QDateEdit(QDate.currentDate().addDays(-29), self._toolbar)
        self._end_date = QDateEdit(QDate.currentDate(), self._toolbar)
        for edit in (self._start_date, self._end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.dateChanged.connect(self._on_history_filter_changed)
            toolbar.addWidget(edit)
        self._quick_date_buttons: list[QPushButton] = []
        for label, days in (("今日", 1), ("近7天", 7), ("近30天", 30)):
            button = QPushButton(label, self._toolbar)
            button.clicked.connect(lambda _checked=False, value=days: self._set_quick_date_range(value))
            self._quick_date_buttons.append(button)
            toolbar.addWidget(button)
        self._cleanup_btn = QPushButton("一键清理", self._toolbar)
        self._cleanup_btn.setObjectName("alchemyClearFileBtn")
        self._cleanup_btn.clicked.connect(self._cleanup_completed_batches)
        toolbar.addWidget(self._cleanup_btn)
        self._eco_settings_btn = QPushButton("API 设置", self._toolbar)
        self._eco_settings_btn.clicked.connect(self._open_eco_settings)
        toolbar.addWidget(self._eco_settings_btn)
        self._eco_help_btn = QPushButton("图文指引", self._toolbar)
        self._eco_help_btn.clicked.connect(self._open_eco_help)
        toolbar.addWidget(self._eco_help_btn)
        self._eco_list_btn = QPushButton("我的合炉", self._toolbar)
        self._eco_list_btn.clicked.connect(self._open_eco_furnaces)
        toolbar.addWidget(self._eco_list_btn)
        main_layout.addWidget(self._toolbar)
        self._eco_status_bar = QWidget(self)
        status_layout = QHBoxLayout(self._eco_status_bar)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self._eco_status_group = QButtonGroup(self)
        self._eco_status_buttons = {}
        self._eco_status_filter = "unlisted"
        for key, text in (("unlisted", "未上合炉"), ("listed", "已上合炉 / 已提交"), ("all", "全部配方")):
            button = QPushButton(text, self._eco_status_bar)
            button.setObjectName("purchaseManageSectionButton")
            button.setCheckable(True)
            button.setChecked(key == "unlisted")
            button.clicked.connect(lambda _checked=False, value=key: self._filter_eco_publication(value))
            self._eco_status_group.addButton(button)
            self._eco_status_buttons[key] = button
            status_layout.addWidget(button)
        status_layout.addStretch(1)
        main_layout.addWidget(self._eco_status_bar)

        self._purchase_batch_empty_label = QLabel("")
        self._purchase_batch_empty_label.setObjectName("alchemyStep1Hint")
        self._purchase_batch_empty_label.setWordWrap(True)
        main_layout.addWidget(self._purchase_batch_empty_label)
        self._purchase_batch_container = QWidget()
        self._purchase_batch_container.setObjectName("alchemyGroupsContainer")
        self._purchase_batch_layout = QVBoxLayout(self._purchase_batch_container)
        self._purchase_batch_layout.setContentsMargins(0, 0, 0, 0)
        self._purchase_batch_layout.setSpacing(8)
        self._purchase_scroll = QScrollArea()
        self._purchase_scroll.setObjectName("alchemyScrollArea")
        self._purchase_scroll.setWidgetResizable(True)
        self._purchase_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._purchase_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._purchase_scroll.setWidget(self._purchase_batch_container)
        main_layout.addWidget(self._purchase_scroll, 1)

        self._icon_refresh_pending = False
        self._apply_page_title_icon()
        self._populate_account_filter()
        self._sync_section_toolbar()
        self._rebuild_purchase_batch_rows()

    def refresh_from_disk(self) -> None:
        preferred = (
            get_active_profile_id()
            if self._current_section == "history"
            else None
        )
        self._populate_account_filter(preferred_profile_id=preferred)
        self._rebuild_purchase_batch_rows()

    def navigation_route_label(self) -> str:
        return next(label for key, label in _SECTIONS if key == self._current_section)

    def navigation_subroute(self) -> str:
        return self._current_section

    def restore_navigation_subroute(self, section: str) -> None:
        self._show_section(section if section in {key for key, _ in _SECTIONS} else "purchasing")

    def _show_section(self, section: str) -> None:
        if section in {"ready", "purchase_completed"}:
            section = "purchasing"
        if section not in {key for key, _label in _SECTIONS}:
            return
        changed = section != self._current_section
        if section == "history":
            self._populate_account_filter(
                preferred_profile_id=get_active_profile_id()
            )
        if section == "eco" and changed:
            self._populate_account_filter(preferred_profile_id="")
        self._current_section = section
        for key, button in self._section_buttons.items():
            button.setProperty("active", key == section)
            button.style().unpolish(button)
            button.style().polish(button)
        self._sync_section_toolbar()
        self._rebuild_purchase_batch_rows()
        if changed:
            self.navigation_route_changed.emit(section)

    def _sync_section_toolbar(self) -> None:
        self._item_search_edit.setVisible(
            self._current_section in {"purchasing", "eco"}
        )
        history = self._current_section == "history"
        eco = self._current_section == "eco"
        self._eco_status_bar.setVisible(eco)
        self._account_combo.setVisible(True)
        self._material_status_combo.setVisible(self._current_section == "purchasing")
        self._alchemy_filter_combo.setVisible(self._current_section == "purchasing")
        self._refresh_ready_btn.setVisible(history or eco)
        for button in (self._eco_settings_btn, self._eco_help_btn, self._eco_list_btn):
            button.setVisible(eco)
        self._refresh_ready_btn.setText("刷新炼金记录" if history else "刷新")
        for widget in (self._start_date, self._end_date, *self._quick_date_buttons):
            widget.setVisible(history)
        self._cleanup_btn.setVisible(self._current_section == "tradeup_completed")
        self._toolbar_label.setText({
            "purchasing": "按采购日期管理配方",
            "tradeup_completed": "按炼金日期查看已汰换配方",
            "history": "以 Steam 库存历史的实际汰换事件统计，按账号和时间查看",
            "eco": "ECO合炉 · 按上炉状态管理配方",
        }[self._current_section])

    def _populate_account_filter(
        self,
        *,
        preferred_profile_id: str | None = None,
    ) -> None:
        try:
            repair_purchase_account_links()
        except (OSError, ValueError):
            # Keep the page usable if a cache file is temporarily locked.
            pass
        current = (
            preferred_profile_id
            if preferred_profile_id is not None
            else self._account_combo.currentData()
        )
        accounts: dict[str, str] = {}
        for entry in list_profile_entries():
            profile_id = str(entry.get("id") or "")
            if profile_id:
                accounts[profile_id] = combo_display_name_for_profile(entry)
        for _path, batch in list_purchase_batches():
            profile_id = str(batch.get("profile_id") or "")
            if profile_id:
                accounts.setdefault(profile_id, str(batch.get("account_name") or "Steam"))
        self._account_combo.blockSignals(True)
        self._account_combo.clear()
        self._account_combo.addItem("全部 Steam 账号", "")
        for profile_id, label in accounts.items():
            self._account_combo.addItem(label, profile_id)
        index = self._account_combo.findData(current)
        self._account_combo.setCurrentIndex(index if index >= 0 else 0)
        self._account_combo.blockSignals(False)

    def _selected_profile_id(self) -> str:
        return str(self._account_combo.currentData() or "")

    def _on_account_filter_changed(self, _index: int) -> None:
        self._rebuild_purchase_batch_rows()

    def _on_refresh_clicked(self) -> None:
        if self._current_section == "history":
            self._refresh_tradeup_history()
        elif self._current_section == "eco":
            self._populate_account_filter()
            self._rebuild_purchase_batch_rows()
        else:
            self._rebuild_purchase_batch_rows()

    def _refresh_tradeup_history(self) -> None:
        if self._history_worker is not None and self._history_worker.isRunning():
            return
        selected = self._selected_profile_id()
        if selected:
            profile_ids = [selected]
        else:
            profile_ids = []
            for index in range(self._account_combo.count()):
                profile_id = str(self._account_combo.itemData(index) or "")
                if profile_id and profile_id not in profile_ids:
                    profile_ids.append(profile_id)
        if not profile_ids:
            show_toast(self, "请先在 Steam 库存添加并登录账号", style="warning")
            return
        self._refresh_ready_btn.setEnabled(False)
        self._refresh_ready_btn.setText("正在读取 Steam 历史…")
        worker = SteamHistorySyncWorker(profile_ids, self, since=self._start_date.date().startOfDay().toPython().astimezone())
        self._history_worker = worker
        worker.status.connect(self._on_history_sync_status)
        worker.completed.connect(self._on_history_sync_completed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_history_sync_status(self, message: str) -> None:
        if self._current_section == "history":
            self._refresh_ready_btn.setText("正在读取…")
            self._refresh_ready_btn.setToolTip(message)

    def _on_history_sync_completed(self, ok: bool, message: str, _updated: int) -> None:
        self._history_worker = None
        self._refresh_ready_btn.setEnabled(True)
        self._refresh_ready_btn.setToolTip("")
        self._sync_section_toolbar()
        if self._current_section == "history":
            self._rebuild_purchase_batch_rows()
        show_toast(self, message, style="success" if ok else "warning")

    def _visible_batch_entries(self) -> list[tuple[Path, dict, set[str] | None]]:
        if self._current_section == "history":
            return []
        visible = []
        for path, payload in list_purchase_batches():
            if self._selected_profile_id() and str(payload.get("profile_id") or "") != self._selected_profile_id():
                continue
            recipes = [entry for entry in payload.get("recipes") or [] if isinstance(entry, dict)]
            selected = [
                entry for entry in recipes
                if (purchase_recipe_section(entry) != "tradeup_completed"
                    if self._current_section == "purchasing"
                    else purchase_recipe_section(entry) == self._current_section)
            ]
            if self._current_section == "purchasing":
                selected = [entry for entry in selected if _matches_alchemy_filter(entry, self._alchemy_filter_combo.currentData())]
                wanted = _FILTER_TO_STATUS[self._material_status_combo.currentText()]
                if wanted:
                    selected = [entry for entry in selected if any(
                        (row.get("status") != "received" if wanted == "not_received" else bool(row.get("inventory_missing_since")) and row.get("status") == "received" if wanted == "missing_review" else row.get("status") == wanted)
                        for row in entry.get("materials") or [])]
            if not selected:
                continue
            copied = copy.deepcopy(payload)
            copied["recipes"] = copy.deepcopy(selected)
            copied["_source_recipe_count"] = len(recipes)
            # 已入库配方可直接进入模拟；实际执行仍由模拟页核对库存和 CD。
            visible.append((path, copied, None))
        return visible

    def _rebuild_purchase_batch_rows(self) -> None:
        self._capture_purchase_batch_ui_state()
        while self._purchase_batch_layout.count():
            item = self._purchase_batch_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if self._current_section == "history":
            self._rebuild_history_rows()
            return
        if self._current_section == "eco":
            self._rebuild_eco_rows()
            return
        entries = self._visible_batch_entries()
        query = (
            self._item_search_edit.text().strip()
            if self._current_section == "purchasing" else ""
        )
        if query:
            entries = [
                (path, payload, ids) for path, payload, ids in entries
                if any(
                    isinstance(entry, dict) and purchase_recipe_matches_item_search(entry, query)
                    for entry in payload.get("recipes") or []
                )
            ]
        self._purchase_batch_empty_label.setText({
            "purchasing": "暂无采购配方，从材料采集加入后会自动归入当天。",
            "tradeup_completed": "暂无已炼金配方；单个配方标记已汰换后会自动归档到这里。",
        }[self._current_section])
        if query:
            self._purchase_batch_empty_label.setText("未找到包含该饰品的配方，请更换关键词或清空搜索。")
        elif self._current_section == "purchasing" and (self._selected_profile_id() or self._material_status_combo.currentIndex() or self._alchemy_filter_combo.currentIndex()):
            self._purchase_batch_empty_label.setText("当前筛选条件下没有配方。")
        self._purchase_batch_empty_label.setVisible(not entries)
        count_entries = [
            (path, dict(payload, recipes=[entry for entry in payload.get("recipes") or [] if purchase_recipe_matches_item_search(entry, query)]))
            for path, payload, _ids in entries
        ] if query else [(path, payload) for path, payload, _ids in entries]
        self._update_purchase_batch_title_count(count_entries)
        days = {}
        for path, payload, ready_ids in entries:
            for entry in payload.get("recipes") or []:
                if query and not purchase_recipe_matches_item_search(entry, query):
                    continue
                stamp = (entry.get("tradeup_completed_at") or (entry.get("tradeup_execution") or {}).get("completed_at")) if self._current_section == "tradeup_completed" else entry.get("added_at")
                moment = _parse_local_datetime(stamp or entry.get("added_at") or payload.get("created_at"))
                day = moment.strftime("%Y-%m-%d") if moment else "日期未记录"
                days.setdefault(day, []).append((path, dict(payload, recipes=[entry]), ready_ids))
        for day in sorted(days, reverse=True):
            rows = days[day]
            cards = []
            for number, (path, payload, ready_ids) in enumerate(rows, 1):
                payload["_day_recipe_number"] = number
                card = self._make_purchase_card(path, payload, ready_ids, query, recipe_only=True)
                cards.append(card)
            self._add_purchase_day(day, cards)
        self._purchase_batch_layout.addStretch(1)
        QTimer.singleShot(0, lambda value=self._purchase_batch_scroll_value: self._purchase_scroll.verticalScrollBar().setValue(value))

    def _add_purchase_day(self, day, cards):
        key = (self._current_section, day)
        expanded = self._purchase_day_expanded.get(key, self._current_section == "eco")
        heading = QPushButton(f"{day} · {len(cards)} 炉")
        heading.setObjectName("purchaseDayHeading")
        heading.setCheckable(True)
        heading.setChecked(expanded)
        heading.setCursor(Qt.PointingHandCursor)
        heading.setIcon(expand_section_triangle_icon(expanded, fill_color="#10b981"))
        if self._current_section in {"purchasing", "tradeup_completed"}:
            bar = QWidget(self._purchase_batch_container)
            bar.setObjectName("recipeManageRow")
            bar.setAttribute(Qt.WA_StyledBackground, True)
            box = QGridLayout(bar)
            box.setContentsMargins(14, 12, 14, 12)
            box.setHorizontalSpacing(12)
            box.setVerticalSpacing(8)
            titles = QVBoxLayout()
            titles.setSpacing(0)
            title_row = QHBoxLayout()
            title_row.setSpacing(24)
            title_row.addWidget(heading)
            account_counts = {}
            for card in cards:
                account = str(card._payload.get("account_name") or "Steam")
                account_counts[account] = account_counts.get(account, 0) + len(card._payload.get("recipes") or [])
            account_label = QLabel(" · ".join(f"{name}：{count}炉" for name, count in account_counts.items()), bar)
            account_label.setObjectName("purchaseDayAccountSummary")
            title_row.addWidget(account_label)
            title_row.addStretch(1)
            titles.addLayout(title_row)
            box.addLayout(titles, 0, 0)
            box.setColumnStretch(0, 1)
            entries = [entry for card in cards for entry in card._payload.get("recipes") or []]
            summary = purchase_batch_summary({"recipes": entries})
            cost = sum(_planned_recipe_cost(entry) or 0 for entry in entries)
            info = QLabel(f"{summary['total']} 件材料 · 已入库 {summary['received']} · 待入库 {summary['ordered']} · 待购买 {summary['pending']} · 需补购 {summary['cancelled']} · 待确认离库 {summary['missing_review']} · 总成本 ¥{cost:.2f}", bar)
            info.setObjectName("alchemyStep1Hint")
            info.setWordWrap(True)
            titles.addWidget(info)
            tools = QHBoxLayout()
            tools.setSpacing(8)
            box.addLayout(tools, 1, 0)
            if self._current_section == "purchasing":
                remaining = [row for entry in entries for row in entry.get("materials") or [] if row.get("status") != "received"]
                undo = bool(remaining) and all(row.get("status") == "ordered" for row in remaining)
                mark = QPushButton("全部撤销已买" if undo else "全部标记已买", bar)
                mark.setObjectName("purchaseDayMarkAllBtn")
                mark.setEnabled(bool(remaining))
                mark.clicked.connect(lambda: self._mark_purchase_day(cards, not undo))
                tools.addWidget(mark)
                change = QPushButton("修改账号", bar)
                change.setObjectName("purchaseDayAccountBtn")
                change.clicked.connect(lambda: self._change_purchase_day_account(day, cards))
                tools.addWidget(change)
            if self._current_section == "purchasing":
                reconcile = QPushButton("核对库存", bar)
                reconcile.setObjectName("purchaseDayReconcileBtn")
                reconcile.setToolTip("使用该日期各收货账号最近刷新到本地的库存核对采购记录")
                reconcile.clicked.connect(lambda: self._reconcile_purchase_day(cards))
                reconcile.setMinimumSize(190, 42)
                box.addWidget(reconcile, 0, 1, 2, 1, Qt.AlignVCenter)
            delete = QPushButton("删除当天配方", bar)
            delete.setObjectName("alchemyClearFileBtn")
            delete.clicked.connect(lambda: self._delete_purchase_day(day, cards))
            tools.addWidget(delete)
            tools.addStretch(1)
            view = QPushButton("收起材料" if expanded else f"查看材料（{summary['total']}）", bar)
            view.setObjectName("purchaseDayViewBtn")
            view.clicked.connect(heading.click)
            heading.toggled.connect(lambda checked: view.setText("收起材料" if checked else f"查看材料（{summary['total']}）"))
            view.setMinimumSize(190, 42)
            box.addWidget(view, 0, 2, 2, 1, Qt.AlignVCenter)
            self._purchase_batch_layout.addWidget(bar)
        else:
            self._purchase_batch_layout.addWidget(heading)
        for card in cards:
            self._purchase_batch_layout.addWidget(card)
            card.setVisible(expanded)
        heading.toggled.connect(lambda checked: self._set_purchase_day_expanded(key, heading, cards, checked))

    def _mark_purchase_day(self, cards, ordered):
        paths = {}
        for card in cards:
            paths.setdefault(card._path, set()).update(str(entry["id"]) for entry in card._payload.get("recipes") or [])
        try:
            for path, ids in paths.items():
                toggle_all_purchase_batch_materials_ordered(path, ids, ordered=ordered)
        except (OSError, ValueError) as exc:
            show_toast(self, f"批量标记失败：{exc}", style="warning")
        self._rebuild_purchase_batch_rows()

    def _change_purchase_day_account(self, day, cards):
        accounts = [entry for entry in list_profile_entries() if entry.get("id")]
        if not accounts:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return
        labels = [str(entry.get("display_name") or "Steam") for entry in accounts]
        label, accepted = QInputDialog.getItem(self, "修改当天配方账号", "目标收货账号：", labels, 0, False)
        if not accepted:
            return
        profile = str(accounts[labels.index(label)]["id"])
        targets = [(card._path, str(entry["id"])) for card in cards if str(card._payload.get("profile_id") or "") != profile for entry in card._payload.get("recipes") or []]
        if not targets or not ask_confirmation(self, "修改当天配方账号", f"将 {day} 当前显示的 {len(targets)} 炉改为账号 {label}？旧账号的入库匹配会撤销，其他日期的配方不变。"):
            return
        cfg = load_steam_account_config_dict(profile)
        try:
            target = daily_purchase_batch(profile_id=profile, steam_id=str(cfg.get("steam_id") or ""), account_name=label, inventory_items=load_profile_inventory_items(profile), day=day)
            for path, entry_id in targets:
                move_purchase_batch_recipe(path, entry_id, target)
        except (OSError, ValueError) as exc:
            show_toast(self, f"修改账号未完成：{exc}", style="warning")
        self._populate_account_filter()
        self._rebuild_purchase_batch_rows()

    def _reconcile_purchase_day(self, cards):
        profiles = {str(card._payload.get("profile_id") or "") for card in cards}
        try:
            for profile in profiles:
                reconcile_all_purchase_records_for_profile(profile, load_profile_inventory_items(profile))
        except (OSError, ValueError) as exc:
            show_toast(self, f"库存核对失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, f"已核对 {len(profiles)} 个账号的本地库存", style="success")

    def _delete_purchase_day(self, day, cards):
        targets = [(card._path, str(entry["id"])) for card in cards for entry in card._payload.get("recipes") or []]
        if not ask_confirmation(self, "删除当天配方", f"确定删除 {day} 当前分类显示的 {len(targets)} 炉及其采购记录？"):
            return
        try:
            for path, entry_id in targets:
                delete_purchase_batch_recipe(path, entry_id)
        except (OSError, ValueError) as exc:
            self._rebuild_purchase_batch_rows()
            show_toast(self, f"删除未完成：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, f"已删除 {len(targets)} 炉", style="success")

    def _set_purchase_day_expanded(self, key, heading, cards, expanded):
        self._purchase_day_expanded[key] = expanded
        heading.setIcon(expand_section_triangle_icon(expanded, fill_color="#10b981"))
        for card in cards:
            card.setVisible(expanded)

    def _eco_eligible_entries(self):
        profile = self._selected_profile_id()
        query = self._item_search_edit.text().strip()
        inventories = {}
        visible = []
        for path, batch in list_purchase_batches():
            batch_profile = str(batch.get("profile_id") or "")
            if profile and profile != batch_profile:
                continue
            if batch_profile not in inventories:
                inventories[batch_profile] = load_profile_inventory_items(batch_profile)
            inventory = inventories[batch_profile]
            for entry in batch.get("recipes") or []:
                if not isinstance(entry, dict) or not purchase_recipe_matches_item_search(entry, query):
                    continue
                if entry.get("tradeup_completed"):
                    continue
                if recipe_publication(batch, entry) or purchase_recipe_eco_readiness(batch, entry, inventory)[0]:
                    visible.append((path, batch, entry))
        return visible

    def _rebuild_eco_rows(self):
        entries = self._eco_eligible_entries()
        listed_count = sum(bool(recipe_publication(batch, entry)) for _path, batch, entry in entries)
        counts = {"all": len(entries), "listed": listed_count, "unlisted": len(entries) - listed_count}
        for key, text in (("unlisted", "未上合炉"), ("listed", "已上合炉 / 已提交"), ("all", "全部配方")):
            self._eco_status_buttons[key].setText(f"{text}（{counts[key]}）")
        if self._eco_status_filter != "all":
            wanted = self._eco_status_filter == "listed"
            entries = [(path, batch, entry) for path, batch, entry in entries if bool(recipe_publication(batch, entry)) == wanted]
        accounts = {str(batch.get("profile_id") or "") for _path, batch, _entry in entries}
        materials = sum(len(entry.get("materials") or []) for _path, _batch, entry in entries)
        category = "可合炉配方" if self._eco_status_filter == "unlisted" else "合炉配方"
        self._title_count_label.setText(f"共{len(accounts)}个账号 · {len(entries)}个{category} · {materials}件材料")
        self._purchase_batch_empty_label.setText("当前分类暂无配方。未上合炉配方需库存齐全且 CD 结束；已提交的配方请切换到“已上合炉 / 已提交”查看。")
        self._purchase_batch_empty_label.setVisible(not entries)
        days = {}
        for path, batch, entry in entries:
            moment = _parse_local_datetime(entry.get("added_at") or batch.get("created_at"))
            day = moment.strftime("%Y-%m-%d") if moment else "日期未记录"
            days.setdefault(day, []).append((path, batch, entry))
        for day in sorted(days, reverse=True):
            cards = []
            for number, (path, batch, entry) in enumerate(days[day], 1):
                card = EcoRecipeCard(path, batch, entry, self._purchase_batch_container, recipe_number=number)
                card.publish_requested.connect(self._publish_eco_recipe)
                card.manage_requested.connect(lambda path=path, entry_id=str(entry.get("id") or ""): self._publish_eco_recipe(path, entry_id))
                card.publication_mark_requested.connect(self._mark_eco_publication)
                cards.append(card)
            self._add_purchase_day(day, cards)
        self._purchase_batch_layout.addStretch(1)

    def _filter_eco_publication(self, value):
        self._eco_status_filter = value
        self._rebuild_purchase_batch_rows()

    def _mark_eco_publication(self, path, entry_id, listed):
        try:
            record_recipe_publication(path, entry_id, "listed" if listed else "unlisted")
        except (OSError, ValueError) as exc:
            show_toast(self, f"合炉状态保存失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()

    def _record_eco_submission(self, dialog, path, entry_id, state, payload, furnace_id):
        try:
            record_recipe_publication(path, entry_id, state, payload=payload, furnace_id=furnace_id)
        except (OSError, ValueError) as exc:
            if state == "pending":
                dialog._create_locked = True
                dialog._set_status(f"合炉状态保存失败，未发送创建请求：{exc}", error=True)
            show_toast(self, f"合炉状态保存失败：{exc}", style="warning")
            return
        QTimer.singleShot(0, self._rebuild_purchase_batch_rows)

    def _publish_eco_recipe(self, path, entry_id):
        from ui.dialogs.eco_furnace_dialog import EcoFurnaceDialog
        try:
            batch = load_purchase_batch(path)
            entry = next(row for row in batch.get("recipes") or [] if str(row.get("id") or "") == entry_id)
            publication = recipe_publication(batch, entry)
            ready, reason = purchase_recipe_eco_readiness(batch, entry, load_profile_inventory_items(str(batch.get("profile_id") or "")))
            if not publication and not ready:
                show_toast(self, reason, style="warning")
                self._rebuild_purchase_batch_rows()
                return
            recipe = purchase_recipe_for_furnace(batch, entry)
        except (OSError, ValueError, StopIteration) as exc:
            show_toast(self, f"配方状态已变化，请刷新后重试：{exc}", style="warning")
            return
        dialog = EcoFurnaceDialog(recipe, self, profile_id=str(batch.get("profile_id") or ""), furnace_id=str(publication.get("furnace_id") or ""), resume=bool(publication), listing_state=publication.get("material_listing"))
        dialog.publication_changed.connect(lambda state, payload, furnace_id: self._record_eco_submission(dialog, path, entry_id, state, payload, furnace_id))
        dialog.exec()
        self._rebuild_purchase_batch_rows()

    def _open_eco_settings(self):
        from ui.dialogs.eco_furnace_dialog import EcoFurnaceDialog
        EcoFurnaceDialog({}, self, initial_tab="settings").exec()

    def _open_eco_furnaces(self):
        from ui.dialogs.eco_furnace_dialog import EcoFurnaceDialog
        EcoFurnaceDialog({}, self, initial_tab="list").exec()
        self._rebuild_purchase_batch_rows()

    def _open_eco_help(self):
        from ui.dialogs.eco_furnace_help_dialog import EcoFurnaceHelpDialog
        EcoFurnaceHelpDialog(self).exec()

    def _card_state_key(self, card: PurchaseBatchCard) -> str:
        ids = sorted(str(entry.get("id") or "") for entry in card._payload.get("recipes") or [])
        return f"{getattr(card, '_purchase_section', self._current_section)}:{card._path}:{','.join(ids)}"

    def _make_purchase_card(self, path, payload, ready_ids, query="", *, recipe_only=False):
        card = PurchaseBatchCard(
            path, payload, self._purchase_batch_container, expanded=False,
            ready_recipe_ids=ready_ids,
            compact_archive=self._current_section == "tradeup_completed",
            recipe_only=recipe_only,
            visible_recipe_ids={str(entry.get("id") or "") for entry in payload.get("recipes") or []},
            allow_recipe_edit=True,
            allow_batch_actions=(
                self._current_section == "purchasing"
                or len(payload.get("recipes") or []) == payload.get("_source_recipe_count", len(payload.get("recipes") or []))
            ),
        )
        card._purchase_section = self._current_section
        state = self._purchase_batch_card_states.get(self._card_state_key(card))
        if self._current_section == "purchasing":
            state = dict(state or {}, filter=self._material_status_combo.currentText())
        card.restore_ui_state(state)
        if recipe_only and not card._expanded:
            card._set_expanded(True)
        if query:
            card.set_item_search(query)
        card.changed.connect(lambda: QTimer.singleShot(0, self._rebuild_purchase_batch_rows))
        card.deleted.connect(self._rebuild_purchase_batch_rows)
        card.change_account_requested.connect(self._change_purchase_batch_account)
        card.edit_recipe_requested.connect(self._edit_purchase_recipe)
        card.simulate_tradeup_requested.connect(self.simulate_tradeup_requested.emit)
        card.import_to_collection_requested.connect(self.import_to_collection_requested.emit)
        return card

    def _edit_purchase_recipe(self, path: Path, entry_id: str):
        try:
            batch = load_purchase_batch(path)
            entry = next(row for row in batch.get("recipes") or [] if str(row.get("id") or "") == entry_id)
        except (OSError, ValueError, StopIteration):
            show_toast(self, "配方已变化，请刷新后重试", style="warning")
            return
        dialog = EditPurchaseRecipeDialog(path, batch, entry, list_purchase_batches(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if dialog.action == "delete":
                if not ask_confirmation(self, "删除该配方", "确定删除这条配方及其采购记录？其他配方不受影响。"):
                    return
                delete_purchase_batch_recipe(path, entry_id)
                message = "已删除该配方"
            elif dialog.action == "move":
                profile = str(dialog.account_combo.currentData() or "")
                if profile == str(batch.get("profile_id") or ""):
                    return
                cfg = load_steam_account_config_dict(profile)
                moment = _parse_local_datetime(entry.get("added_at") or batch.get("created_at"))
                target = daily_purchase_batch(
                    profile_id=profile, steam_id=str(cfg.get("steam_id") or ""),
                    account_name=dialog.account_combo.currentText(), inventory_items=[],
                    day=moment.strftime("%Y-%m-%d") if moment else "",
                )
                reset = move_purchase_batch_recipe(path, entry_id, Path(target))
                message = "配方已转移" + (f"，请在新账号下重新核对 {reset} 件材料" if reset else "")
            else:
                return
        except (OSError, ValueError) as exc:
            show_toast(self, f"编辑失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, message, style="success")

    def _rebuild_history_rows(self) -> None:
        start = self._start_date.date().startOfDay().toPython()
        end = self._end_date.date().addDays(1).startOfDay().toPython()
        all_records = list_steam_tradeup_records(self._selected_profile_id())
        records = []
        for record in all_records:
            completed = _parse_local_datetime(record.get("completed_at"))
            if completed is not None and start <= completed.replace(tzinfo=None) < end:
                records.append(record)
        self._purchase_batch_empty_label.setText("当前账号和时间范围内暂无 Steam 汰换历史，请点击“刷新炼金记录”。")
        self._purchase_batch_empty_label.setVisible(not records)
        waiting_count = sum(not row.get("products") for row in records)
        suffix = f" · {waiting_count}条产物待识别" if waiting_count else ""
        self._title_count_label.setText(f"共{len(records)}条记录{suffix}")
        if records:
            self._purchase_batch_layout.addWidget(
                self._build_history_dashboard(records, all_records)
            )
            self._purchase_batch_layout.addWidget(
                self._history_section_heading("汰换结果展示")
            )
        grouped: dict[tuple[str, str], list[dict]] = {}
        for record in records:
            key = (str(record.get("profile_id") or ""), str(record.get("account_name") or "Steam"))
            grouped.setdefault(key, []).append(record)
        for (_profile, account_name), account_records in grouped.items():
            account = QLabel(f"Steam 账号：{account_name}")
            account.setTextFormat(Qt.TextFormat.PlainText)
            account.setObjectName("recipeSavedTitle")
            self._purchase_batch_layout.addWidget(account)
            by_day: dict[str, list[dict]] = {}
            for record in account_records:
                completed = _parse_local_datetime(record.get("completed_at"))
                day_key = completed.strftime("%Y-%m-%d") if completed else "时间未知"
                by_day.setdefault(day_key, []).append(record)
            for day_key, day_records in by_day.items():
                self._purchase_batch_layout.addWidget(
                    self._build_history_day_header(day_key, day_records)
                )
                for record in day_records:
                    self._purchase_batch_layout.addWidget(self._build_history_card(record))
        self._purchase_batch_layout.addStretch(1)

    def _build_history_card(self, record: dict) -> QFrame:
        card = QFrame(self._purchase_batch_container)
        card.setObjectName("tradeupHistoryCompactRow")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(6)
        completed = _parse_local_datetime(record.get("completed_at"))
        when = completed.strftime("%H:%M") if completed else "时间未知"
        try:
            recipe_index = max(1, int(record.get("recipe_index") or 1))
        except (TypeError, ValueError):
            recipe_index = 1
        details = (
            f"{record['batch_name']}　·　配方 {recipe_index:02d}"
            if record.get("batch_name") else "Steam 库存历史"
        )
        metadata = QLabel(f"汰换时间 {when}　·　{details}")
        metadata.setObjectName("tradeupHistoryRecordMeta")
        layout.addWidget(metadata)
        profit = record.get("profit")
        if profit is None:
            profit_text, color = ("等待材料成本" if record.get("material_cost") is None else "等待产物价格"), "#94a3b8"
        else:
            value = float(profit)
            profit_text = f"{'↑' if value >= 0 else '↓'} ¥{value:+.2f}"
            color = "#10b981" if value >= 0 else "#ef4444"
        money_row = QHBoxLayout()
        money_row.setSpacing(22)
        cost_caption = "参考成本" if record.get("material_cost_source") == "market_reference" else "成本"
        cost_label = QLabel(f"{cost_caption}：{_money(record.get('material_cost'))}", card)
        cost_label.setObjectName("tradeupHistoryMoneyLabel")
        money_row.addWidget(cost_label)
        output_label = QLabel(f"产物：{_money(record.get('output_value'))}", card)
        output_label.setObjectName("tradeupHistoryMoneyLabel")
        money_row.addWidget(output_label)
        money_row.addStretch(1)
        profit_label = QLabel(profit_text, card)
        profit_label.setObjectName("tradeupHistoryRecipeProfit")
        profit_label.setStyleSheet(f"color: {color};")
        money_row.addWidget(profit_label)
        layout.addLayout(money_row)

        result_row = QHBoxLayout()
        result_row.setSpacing(10)
        materials_host = QWidget(card)
        materials_host.setObjectName("tradeupHistoryMaterialGrid")
        materials_grid = QGridLayout(materials_host)
        materials_grid.setContentsMargins(0, 0, 0, 0)
        materials_grid.setHorizontalSpacing(4)
        materials_grid.setVerticalSpacing(4)
        materials = [row for row in record.get("materials") or [] if isinstance(row, dict)]
        for index, item in enumerate(materials):
            materials_grid.addWidget(
                self._build_history_item_thumb(item, color, product=False),
                index // 5,
                index % 5,
            )
        result_row.addWidget(materials_host, 1)
        arrow = QLabel("→", card)
        arrow.setObjectName("tradeupHistoryArrow")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_row.addWidget(arrow)
        products = [row for row in record.get("products") or [] if isinstance(row, dict)]
        if products:
            product_card = self._build_history_item_thumb(products[0], color, product=True)
            product_card.clicked.connect(
                lambda: self._show_history_product(products[0], profit)
            )
            result_row.addWidget(product_card)
        else:
            pending = QFrame(card)
            pending.setObjectName("tradeupHistoryProductPending")
            pending_layout = QVBoxLayout(pending)
            pending_layout.setContentsMargins(10, 8, 10, 8)
            pending_label = QLabel("产物待识别\n点击刷新炼金记录", pending)
            pending_label.setObjectName("alchemyStep1Hint")
            pending_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pending_layout.addWidget(pending_label)
            result_row.addWidget(pending)
        layout.addLayout(result_row)
        return card

    def _show_history_product(self, product: dict, profit) -> None:
        from ui.dialogs.steam_tradeup_result_dialog import SteamTradeupResultDialog

        SteamTradeupResultDialog(
            self, [product], profit=profit, history_preview=True,
            image_path=self._history_item_image_path(product)
        ).exec()

    def _build_history_dashboard(
        self,
        records: list[dict],
        all_records: list[dict],
    ) -> QFrame:
        total_cost = sum(float(row.get("material_cost") or 0) for row in records)
        known = [row for row in records if row.get("profit") is not None]
        valued = [row for row in records if row.get("output_value") is not None]
        costed = [row for row in records if row.get("material_cost") is not None]
        total_output = sum(float(row["output_value"]) for row in valued)
        total_profit = sum(float(row.get("profit") or 0) for row in known)
        breakeven = sum(1 for row in known if float(row.get("profit") or 0) >= 0)
        target_results = [result for row in records if (result := self._target_success(row)) is not None]
        target_hits = sum(1 for result in target_results if result)
        dashboard = QFrame(self._purchase_batch_container)
        dashboard.setObjectName("tradeupHistoryDashboard")
        layout = QVBoxLayout(dashboard)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._history_section_heading("汰换数据", dashboard))
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(8)
        metrics.setVerticalSpacing(8)
        metric_rows = (
            (str(len(records)), "汰换次数", ""),
            (
                f"{target_hits / len(target_results):.1%}" if target_results else "--",
                "成功率",
                "命中特殊磨损目标的比例；普通配方显示 --",
            ),
            (
                f"{breakeven / len(known):.1%}" if known else "--",
                "保本率",
                "成本和产物价值均已知的记录中，产物价值不低于成本的比例",
            ),
            (
                f"¥{total_profit:+.2f}" if known else "--",
                "汰换收益",
                "",
            ),
            (f"¥{total_cost:.2f}" if costed else "--", "汰换总成本价", "仅汇总已知成本；参考成本取当前本地价格，非实际成交价"),
            (f"¥{total_output:.2f}" if valued else "--", "汰换总价值", "仅汇总已知产物参考价值"),
        )
        for index, (value, label, tooltip) in enumerate(metric_rows):
            tile = self._build_history_metric_tile(value, label, tooltip)
            if label == "汰换收益" and known:
                tile.setProperty("outcome", "profit" if total_profit >= 0 else "loss")
            metrics.addWidget(tile, index // 3, index % 3)
        layout.addLayout(metrics)
        layout.addWidget(self._history_section_heading("收益走势", dashboard))
        controls = QHBoxLayout()
        controls.addStretch(1)
        for days, label in ((7, "7天"), (30, "一个月"), (183, "近半年")):
            button = QPushButton(label, dashboard)
            button.setObjectName("tradeupHistoryTrendButton")
            button.setCheckable(True)
            button.setChecked(days == self._history_trend_days)
            button.setProperty("active", days == self._history_trend_days)
            button.clicked.connect(
                lambda _checked=False, value=days: self._set_history_trend_days(value)
            )
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)
        layout.addWidget(_ProfitTrendChart(self._history_trend_points(all_records), dashboard))
        return dashboard

    @staticmethod
    def _history_section_heading(text: str, parent=None) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("tradeupHistorySectionHeading")
        return label

    @staticmethod
    def _build_history_metric_tile(value: str, label: str, tooltip: str) -> QFrame:
        tile = QFrame()
        tile.setObjectName("tradeupHistoryMetricTile")
        tile.setToolTip(tooltip)
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(8, 11, 8, 11)
        layout.setSpacing(3)
        value_label = QLabel(value, tile)
        value_label.setObjectName("tradeupHistoryMetricValue")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(value_label)
        caption = QLabel(label, tile)
        caption.setObjectName("tradeupHistoryMetricCaption")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption)
        return tile

    def _build_history_day_header(self, day: str, records: list[dict]) -> QFrame:
        header = QFrame(self._purchase_batch_container)
        header.setObjectName("tradeupHistoryDayHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 7, 12, 7)
        date_label = QLabel(day, header)
        date_label.setObjectName("tradeupHistoryDayLabel")
        layout.addWidget(date_label)
        layout.addStretch(1)
        known = [row for row in records if row.get("profit") is not None]
        if known:
            profit = sum(float(row.get("profit") or 0) for row in known)
            text = f"当天收益：{'↑' if profit >= 0 else '↓'} ¥{profit:+.2f}"
            profit_label = QLabel(text, header)
            profit_label.setStyleSheet(
                f"color: {'#10b981' if profit >= 0 else '#ef4444'};"
            )
        else:
            profit_label = QLabel("当天收益：等待成本或产物价格", header)
        profit_label.setObjectName("tradeupHistoryDayProfit")
        layout.addWidget(profit_label)
        return header

    def _build_history_item_thumb(
        self,
        item: dict,
        price_color: str,
        *,
        product: bool,
    ) -> QFrame:
        card = (_HistoryProductCard if product else QFrame)(self._purchase_batch_container)
        card.setObjectName(
            "tradeupHistoryProductCard" if product else "tradeupHistoryItemCard"
        )
        card.setFixedWidth(176 if product else 108)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)
        image_label = QLabel(card)
        image_label.setObjectName("tradeupHistoryItemImage")
        image_label.setFixedHeight(78 if product else 48)
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_path = self._history_item_image_path(item)
        pixmap = QPixmap(image_path) if image_path else QPixmap()
        if not pixmap.isNull():
            image_label.setPixmap(
                pixmap.scaled(
                    158 if product else 94,
                    74 if product else 45,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            image_label.setText("暂无图片")
        layout.addWidget(image_label)
        full_name = str(item.get("name") or "未知饰品")
        template = resolve_inventory_skin_template(item)
        if template is not None:
            full_name = f"{template.weapon_name} | {template.skin_name}" if template.skin_name else template.weapon_name
            market_name = canonical_steam_market_hash_name(str(item.get("market_hash_name") or ""))
            appearance = next((wear for wear, name in template.steam.items() if canonical_steam_market_hash_name(name) == market_name), None)
            if appearance:
                full_name += f"（{appearance}）"
        name_label = QLabel(card)
        name_label.setObjectName("tradeupHistoryItemName")
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setText(
            QFontMetrics(name_label.font()).elidedText(
                full_name,
                Qt.TextElideMode.ElideRight,
                160 if product else 96,
            )
        )
        name_label.setToolTip(full_name)
        layout.addWidget(name_label)
        price_label = QLabel(_money(item.get("price")), card)
        if item.get("price_source") == "wear_market_reference":
            price_label.setText("参考 " + _money(item.get("price")))
            price_label.setToolTip("Steam 历史无精确磨损，使用该外观档位的本地最低参考价，非实际成交价。")
        price_label.setObjectName("tradeupHistoryItemPrice")
        price_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price_label.setStyleSheet(f"color: {price_color};")
        layout.addWidget(price_label)
        card.setToolTip(f"{full_name}\n{_money(item.get('price'))}")
        return card

    def _history_trend_points(self, records: list[dict]) -> list[tuple[date, float]]:
        end_day = self._end_date.date().toPython()
        start_day = end_day - timedelta(days=self._history_trend_days - 1)
        daily = {start_day + timedelta(days=index): 0.0 for index in range(self._history_trend_days)}
        has_known = False
        for record in records:
            completed = _parse_local_datetime(record.get("completed_at"))
            if completed is None or record.get("profit") is None:
                continue
            day = completed.date()
            if day in daily:
                daily[day] += float(record.get("profit") or 0)
                has_known = True
        return list(daily.items()) if has_known else []

    def _set_history_trend_days(self, days: int) -> None:
        if days == self._history_trend_days:
            return
        self._history_trend_days = days
        self._rebuild_purchase_batch_rows()

    @staticmethod
    def _target_success(record: dict) -> bool | None:
        target = str(record.get("target_paint_index") or "")
        products = [row for row in record.get("products") or [] if isinstance(row, dict)]
        if not target or not products:
            return None
        template = get_template_from_goods_name(str(products[0].get("name") or ""))
        return bool(template is not None and str(template.paint_index) == target)

    @staticmethod
    def _history_item_image_path(item: dict) -> str:
        name = str(item.get("name") or "")
        template = get_template_from_goods_name(name)
        if template is None:
            template = resolve_inventory_skin_template(
                {
                    "name": name,
                    "market_name": name,
                    "market_hash_name": name,
                }
            )
        path = weapon_image_path_from_skin_template(template) if template is not None else None
        if not path:
            path = resolve_inventory_item_icon_path(item)
        return str(path or "")

    def _set_quick_date_range(self, days: int) -> None:
        today = QDate.currentDate()
        self._start_date.blockSignals(True)
        self._end_date.blockSignals(True)
        self._start_date.setDate(today.addDays(-(days - 1)))
        self._end_date.setDate(today)
        self._start_date.blockSignals(False)
        self._end_date.blockSignals(False)
        self._rebuild_purchase_batch_rows()

    def _on_history_filter_changed(self, _date: QDate) -> None:
        if self._current_section == "history":
            self._rebuild_purchase_batch_rows()

    def _cleanup_completed_batches(self) -> None:
        completed = sum(
            1 for _path, batch in list_purchase_batches() for entry in batch.get("recipes") or []
            if entry.get("tradeup_completed")
        )
        if not completed:
            show_toast(self, "没有可清理的已炼金配方", style="info")
            return
        if not ask_confirmation(self, "清理已炼金配方", f"确定清理全部 {completed} 个已炼金配方？统计记录会保留，其他配方不受影响。"):
            return
        try:
            deleted = delete_tradeup_completed_batches()
        except (OSError, ValueError) as exc:
            show_toast(self, f"清理失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, f"已清理 {deleted} 个已炼金配方", style="success")

    def _capture_purchase_batch_ui_state(self) -> None:
        self._purchase_batch_scroll_value = self._purchase_scroll.verticalScrollBar().value()
        for index in range(self._purchase_batch_layout.count()):
            widget = self._purchase_batch_layout.itemAt(index).widget()
            cards = []
            if isinstance(widget, PurchaseBatchCard):
                cards = [widget]
            for card in cards:
                self._purchase_batch_card_states[self._card_state_key(card)] = card.ui_state()

    def _change_purchase_batch_account(self, path: Path) -> None:
        entries = [entry for entry in list_profile_entries() if str(entry.get("id") or "")]
        if not entries:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return
        try:
            batch = next(payload for batch_path, payload in list_purchase_batches() if batch_path == path)
        except StopIteration:
            show_toast(self, "采购批次已不存在", style="warning")
            return
        labels = [combo_display_name_for_profile(entry) for entry in entries]
        current_id = str(batch.get("profile_id") or "")
        current_index = next((i for i, entry in enumerate(entries) if str(entry.get("id") or "") == current_id), 0)
        selected, accepted = QInputDialog.getItem(self, "修改收货账号", "Steam 收货账号：", labels, current_index, False)
        if not accepted:
            return
        profile_id = str(entries[labels.index(selected)].get("id") or "")
        if profile_id == current_id:
            show_toast(self, "收货账号没有变化", style="info")
            return
        if not ask_confirmation(self, "确认修改收货账号", "修改后会以新账号当前库存重新建立入库基线；旧账号已经匹配的入库记录将撤销。确定继续吗？"):
            return
        cfg = load_steam_account_config_dict(profile_id)
        try:
            reset_count = update_purchase_batch_account(
                path, profile_id=profile_id, steam_id=str(cfg.get("steam_id") or ""),
                account_name=selected, inventory_items=load_profile_inventory_items(profile_id),
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"收货账号修改失败：{exc}", style="warning")
            return
        self._populate_account_filter()
        self._rebuild_purchase_batch_rows()
        message = f"收货账号已改为 {selected}"
        if reset_count:
            message += f"，已撤销 {reset_count} 件旧账号入库匹配"
        show_toast(self, message, style="success")

    def _update_purchase_batch_title_count(self, entries=None) -> None:
        if entries is None:
            entries = [(path, payload) for path, payload, _ids in self._visible_batch_entries()]
        recipe_total = sum(len(payload.get("recipes") or []) for _path, payload in entries)
        material_total = sum(int(purchase_batch_summary(payload).get("total") or 0) for _path, payload in entries)
        self._title_count_label.setText(f"共{recipe_total}炉 · {material_total}件材料")

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_page_title_icon()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() not in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            return
        if not self.isVisible() or self._icon_refresh_pending:
            return
        self._icon_refresh_pending = True
        QTimer.singleShot(0, self._run_icon_refresh)

    def _run_icon_refresh(self) -> None:
        self._icon_refresh_pending = False
        if self.isVisible():
            self._apply_page_title_icon()

    def _apply_page_title_icon(self) -> None:
        if not RECIPE_ICON_PATH.is_file():
            return
        color = self.palette().color(QPalette.ColorRole.WindowText).name()
        pixmap = load_svg_icon(RECIPE_ICON_PATH, color, size=28).pixmap(28, 28)
        if pixmap is not None and not pixmap.isNull():
            self._page_title_icon_label.setPixmap(pixmap)
