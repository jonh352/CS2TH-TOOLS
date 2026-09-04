"""采购管理页：采购、可炼金、归档与账号维度炼金统计。"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen,
    QPixmap, QShowEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDateEdit, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from config import CONTENT_PAGE_LAYOUT_MARGINS, RECIPE_ICON_PATH
from core.inventory_steam_accounts import (
    combo_display_name_for_profile, get_active_profile_id, list_profile_entries,
    load_steam_account_config_dict,
)
from core.alchemy_quality import get_template_from_goods_name, resolve_inventory_skin_template
from core.inventory_icons import resolve_inventory_item_icon_path, weapon_image_path_from_skin_template
from core.purchase_batches import (
    create_purchase_batch, delete_tradeup_completed_batches, list_purchase_batches,
    list_ready_purchase_batch_recipes, list_tradeup_records, purchase_batch_section,
    purchase_batch_summary, update_purchase_batch_account,
)
from core.purchase_tracking import load_profile_inventory_items
from ui.dialogs.wide_text_input_dialog import get_wide_text_input
from ui.feedback import ask_confirmation
from ui.icons import load_svg_icon
from ui.widgets.purchase_batch_card import PurchaseBatchCard
from ui.widgets.toast import show_toast
from ui.workers.steam_history import SteamHistorySyncWorker


_SECTIONS = (
    ("purchasing", "采购管理"),
    ("ready", "可炼金配方"),
    ("purchase_completed", "已采购完成"),
    ("tradeup_completed", "已炼金批次"),
    ("history", "汰换记录与统计"),
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
    navigation_route_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("purchaseManagePage")
        self._current_section = "purchasing"
        self._purchase_batch_card_states: dict[str, dict[str, object]] = {}
        self._purchase_batch_scroll_value = 0
        self._ready_entries: list[tuple[Path, dict, list[str]]] = []
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
        self._title_count_label = QLabel("共0个批次 · 0个配方 · 0件材料")
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
        self._account_combo = QComboBox(self._toolbar)
        self._account_combo.setMinimumWidth(160)
        self._account_combo.currentIndexChanged.connect(self._on_account_filter_changed)
        toolbar.addWidget(self._account_combo)
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
        self._new_purchase_batch_btn = QPushButton("新建采购批次")
        self._new_purchase_batch_btn.setObjectName("alchemySelectFileBtn")
        self._new_purchase_batch_btn.clicked.connect(self._create_purchase_batch)
        toolbar.addWidget(self._new_purchase_batch_btn)
        main_layout.addWidget(self._toolbar)

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
            if self._current_section in {"ready", "history"}
            else None
        )
        self._populate_account_filter(preferred_profile_id=preferred)
        if self._current_section == "ready":
            self._refresh_ready_recipes()
        else:
            self._rebuild_purchase_batch_rows()

    def navigation_route_label(self) -> str:
        return next(label for key, label in _SECTIONS if key == self._current_section)

    def navigation_subroute(self) -> str:
        return self._current_section

    def restore_navigation_subroute(self, section: str) -> None:
        self._show_section(section if section in {key for key, _ in _SECTIONS} else "purchasing")

    def _show_section(self, section: str) -> None:
        if section not in {key for key, _label in _SECTIONS}:
            return
        changed = section != self._current_section
        if section in {"ready", "history"}:
            self._populate_account_filter(
                preferred_profile_id=get_active_profile_id()
            )
        self._current_section = section
        for key, button in self._section_buttons.items():
            button.setProperty("active", key == section)
            button.style().unpolish(button)
            button.style().polish(button)
        if section != "ready":
            self._ready_entries = []
        self._sync_section_toolbar()
        if section == "ready":
            self._refresh_ready_recipes()
        else:
            self._rebuild_purchase_batch_rows()
        if changed:
            self.navigation_route_changed.emit(section)

    def _sync_section_toolbar(self) -> None:
        ready = self._current_section == "ready"
        history = self._current_section == "history"
        self._account_combo.setVisible(ready or history)
        self._refresh_ready_btn.setVisible(ready or history)
        self._refresh_ready_btn.setText("刷新炼金记录" if history else "刷新")
        for widget in (self._start_date, self._end_date, *self._quick_date_buttons):
            widget.setVisible(history)
        self._cleanup_btn.setVisible(self._current_section == "tradeup_completed")
        self._new_purchase_batch_btn.setVisible(self._current_section == "purchasing")
        self._toolbar_label.setText({
            "purchasing": "尚未全部入库的采购批次",
            "ready": "选择 Steam 账号并刷新，核对每个配方的库存与 CD",
            "purchase_completed": "材料已全部入库的批次",
            "tradeup_completed": "所有配方均已炼金完成的批次",
            "history": "按 Steam 账号和时间查看汰换材料、真实产物及收益",
        }[self._current_section])

    def _populate_account_filter(
        self,
        *,
        preferred_profile_id: str | None = None,
    ) -> None:
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
        if self._current_section == "ready":
            self._ready_entries = []
        if self._current_section in {"ready", "history"}:
            self._rebuild_purchase_batch_rows()

    def _refresh_ready_recipes(self) -> None:
        self._ready_entries = list_ready_purchase_batch_recipes(self._selected_profile_id())
        self._rebuild_purchase_batch_rows()
        count = sum(len(ids) for _path, _batch, ids in self._ready_entries)
        show_toast(self, f"刷新完成，共 {count} 个配方库存齐全且 CD 已结束", style="success" if count else "info")

    def _on_refresh_clicked(self) -> None:
        if self._current_section == "history":
            self._refresh_tradeup_history()
        else:
            self._refresh_ready_recipes()

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
        worker = SteamHistorySyncWorker(profile_ids, self)
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

    def _create_purchase_batch(self) -> None:
        accounts = [entry for entry in list_profile_entries() if str(entry.get("id") or "")]
        if not accounts:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return
        if not ask_confirmation(self, "创建采购批次", "创建时会把该账号当前本地库存记为基线。请确认已经在 Steam 库存页刷新过该账号库存。"):
            return
        name, accepted = get_wide_text_input(
            self, title="新建采购批次", label="批次名称：",
            value=datetime.now().strftime("采购批次 %Y-%m-%d %H:%M"),
        )
        if not accepted or not name.strip():
            return
        labels = [combo_display_name_for_profile(entry) for entry in accounts]
        active_id = get_active_profile_id()
        current = next((i for i, entry in enumerate(accounts) if str(entry.get("id") or "") == active_id), 0)
        account_label, accepted = QInputDialog.getItem(self, "选择收货账号", "Steam 收货账号：", labels, current, False)
        if not accepted:
            return
        entry = accounts[labels.index(account_label)]
        profile_id = str(entry.get("id") or "")
        cfg = load_steam_account_config_dict(profile_id)
        try:
            create_purchase_batch(
                name, profile_id=profile_id, steam_id=str(cfg.get("steam_id") or ""),
                account_name=account_label, inventory_items=load_profile_inventory_items(profile_id),
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"采购批次创建失败：{exc}", style="warning")
            return
        self._populate_account_filter()
        self._rebuild_purchase_batch_rows()
        show_toast(self, "采购批次已创建，可从材料采集加入配方", style="success")

    def _visible_batch_entries(self) -> list[tuple[Path, dict, set[str] | None]]:
        if self._current_section == "ready":
            visible = []
            for path, payload, ids in self._ready_entries:
                copied = copy.deepcopy(payload)
                copied["recipes"] = [entry for entry in copied.get("recipes") or [] if isinstance(entry, dict) and str(entry.get("id") or "") in ids]
                visible.append((path, copied, set(ids)))
            return visible
        if self._current_section == "history":
            return []
        return [
            (path, payload, set() if self._current_section == "purchase_completed" else None)
            for path, payload in list_purchase_batches()
            if purchase_batch_section(payload) == self._current_section
        ]

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
        entries = self._visible_batch_entries()
        valid_keys = {str(path) for path, _payload, _ready in entries}
        self._purchase_batch_card_states = {key: state for key, state in self._purchase_batch_card_states.items() if key in valid_keys}
        self._purchase_batch_empty_label.setText({
            "purchasing": "暂无采购中的批次。新建批次后，可从材料采集把配方加入这里。",
            "ready": "暂无已校验的可炼金配方。请选择 Steam 账号并点击“刷新”。",
            "purchase_completed": "暂无已采购完成批次；批次材料全部入库后会自动转移到这里。",
            "tradeup_completed": "暂无已炼金批次；批次内所有配方完成后会自动归档到这里。",
        }[self._current_section])
        self._purchase_batch_empty_label.setVisible(not entries)
        self._update_purchase_batch_title_count([(path, payload) for path, payload, _ids in entries])
        for path, payload, ready_ids in entries:
            card = PurchaseBatchCard(
                path, payload, self._purchase_batch_container, expanded=False,
                ready_recipe_ids=ready_ids,
                compact_archive=self._current_section == "tradeup_completed",
            )
            card.restore_ui_state(self._purchase_batch_card_states.get(str(path)))
            card.changed.connect(lambda: QTimer.singleShot(0, self._rebuild_purchase_batch_rows))
            card.deleted.connect(self._rebuild_purchase_batch_rows)
            card.change_account_requested.connect(self._change_purchase_batch_account)
            card.simulate_tradeup_requested.connect(lambda plan: self.simulate_tradeup_requested.emit(plan))
            self._purchase_batch_layout.addWidget(card)
        self._purchase_batch_layout.addStretch(1)
        QTimer.singleShot(0, lambda value=self._purchase_batch_scroll_value: self._purchase_scroll.verticalScrollBar().setValue(value))

    def _rebuild_history_rows(self) -> None:
        start = self._start_date.date().startOfDay().toPython()
        end = self._end_date.date().addDays(1).startOfDay().toPython()
        all_records = list_tradeup_records(self._selected_profile_id())
        records = []
        for record in all_records:
            completed = _parse_local_datetime(record.get("completed_at"))
            if completed is not None and start <= completed.replace(tzinfo=None) < end:
                records.append(record)
        self._purchase_batch_empty_label.setText("当前账号和时间范围内暂无炼金记录。")
        self._purchase_batch_empty_label.setVisible(not records)
        known_output = [row for row in records if row.get("output_value") is not None]
        waiting_count = len(records) - len(known_output)
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
        metadata = QLabel(
            f"汰换时间 {when}　·　{str(record.get('batch_name') or '采购批次')}　·　"
            f"配方 {recipe_index:02d}"
        )
        metadata.setObjectName("tradeupHistoryRecordMeta")
        layout.addWidget(metadata)
        profit = record.get("profit")
        if profit is None:
            profit_text, color = "等待产物价格", "#94a3b8"
        else:
            value = float(profit)
            profit_text = f"{'↑' if value >= 0 else '↓'} ¥{value:+.2f}"
            color = "#10b981" if value >= 0 else "#ef4444"
        money_row = QHBoxLayout()
        money_row.setSpacing(22)
        cost_label = QLabel(f"成本：{_money(record.get('material_cost'))}", card)
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
            result_row.addWidget(
                self._build_history_item_thumb(products[0], color, product=True)
            )
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

    def _build_history_dashboard(
        self,
        records: list[dict],
        all_records: list[dict],
    ) -> QFrame:
        total_cost = sum(float(row.get("material_cost") or 0) for row in records)
        known = [row for row in records if row.get("output_value") is not None]
        total_output = sum(float(row.get("output_value") or 0) for row in known)
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
                "产物价值不低于材料成本的比例",
            ),
            (
                f"¥{total_profit:+.2f}" if known else "--",
                "汰换收益",
                "",
            ),
            (f"¥{total_cost:.2f}", "汰换总成本价", ""),
            (f"¥{total_output:.2f}" if known else "--", "汰换总价值", ""),
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
            profit_label = QLabel("当天收益：等待产物价格", header)
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
        card = QFrame(self._purchase_batch_container)
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
        entries = [payload for _path, payload in list_purchase_batches() if purchase_batch_section(payload) == "tradeup_completed"]
        if not entries:
            show_toast(self, "没有可清理的已炼金批次", style="info")
            return
        if not ask_confirmation(self, "一键清理已炼金批次", f"确定删除全部 {len(entries)} 个已炼金批次及其本地记录？"):
            return
        try:
            deleted = delete_tradeup_completed_batches()
        except (OSError, ValueError) as exc:
            show_toast(self, f"清理失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, f"已清理 {deleted} 个已炼金批次", style="success")

    def _capture_purchase_batch_ui_state(self) -> None:
        self._purchase_batch_scroll_value = self._purchase_scroll.verticalScrollBar().value()
        for index in range(self._purchase_batch_layout.count()):
            card = self._purchase_batch_layout.itemAt(index).widget()
            if isinstance(card, PurchaseBatchCard):
                self._purchase_batch_card_states[str(card._path)] = card.ui_state()

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
        self._title_count_label.setText(f"共{len(entries)}个批次 · {recipe_total}个配方 · {material_total}件材料")

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
