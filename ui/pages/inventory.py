"""Responsive Steam inventory page with multi-account sessions and lazy icons."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from core.alchemy_quality import resolve_inventory_skin_template
from core.data_utils import APPEARANCE_MAP, inventory_wear_chinese
from core.inventory_icons import resolve_inventory_item_icon_path
from core.inventory_steam_accounts import (
    combo_display_name_for_profile,
    commit_pending_steam_profile,
    delete_steam_profile,
    discard_pending_add_account_root,
    get_active_profile_id,
    list_profile_entries,
    load_steam_account_config_dict,
    pending_add_account_root,
    prepare_pending_add_account_root,
    profile_inventory_data_path,
    profile_session_root,
    save_steam_account_config_dict,
    set_active_profile,
    update_profile_display_name,
)
from core.platform_links import MARKETPLACES, links_for_template
from core.purchase_batches import reconcile_all_purchase_records_for_profile
from config import CONTENT_PAGE_LAYOUT_MARGINS
from ui.components import PageHeader, panel
from ui.feedback import ask_confirmation
from ui.weapon_card_image_area import line_and_tint_for_quality_cn
from ui.widgets.float_line_edit import format_float_shortest

# 与本地 weapon_images（约 220×140）及炼金模拟枪图槽比例一致
_INV_WEAPON_ICON_W = 176
_INV_WEAPON_ICON_H = 112
_INV_IMAGE_AREA_H = 8 + _INV_WEAPON_ICON_H + 3
_INV_CARD_INNER_W = 8 + _INV_WEAPON_ICON_W + 8
# 边距 8×2 + 图区 + spacing + 名称/磨损/价格/状态
_INV_CARD_H = 8 + _INV_IMAGE_AREA_H + 6 + 34 + 6 + 16 + 4 + 16 + 4 + 16 + 8
_INV_GRID_W = _INV_CARD_INNER_W + 12
_INV_GRID_H = _INV_CARD_H + 12
# Item widgets are intentionally created in event-loop-sized chunks. Building a
# large Steam inventory in one pass can otherwise freeze navigation for >1s.
_INV_RENDER_BATCH_SIZE = 8
_ROLE_NAME = int(Qt.ItemDataRole.UserRole) + 1
_ROLE_WEAR = _ROLE_NAME + 1
_ROLE_STATUS = _ROLE_NAME + 2
_ROLE_QUALITY = _ROLE_NAME + 3
_ROLE_ICON = _ROLE_NAME + 4
_ROLE_ICON_LOADED = _ROLE_NAME + 5
_ROLE_PRICE = _ROLE_NAME + 6

_QUALITY_RANK_CN: dict[str, int] = {
    "消费级": 0,
    "工业级": 1,
    "军规级": 2,
    "受限": 3,
    "保密": 4,
    "隐秘": 5,
    "非凡": 6,
}
# Steam inventory ``rarity`` 后缀 → 排序档（与品质色条一致）
_STEAM_RARITY_RANK: dict[str, int] = {
    "common": 0,
    "common_weapon": 0,
    "uncommon": 1,
    "uncommon_weapon": 1,
    "rare": 2,
    "rare_weapon": 2,
    "mythical": 3,
    "mythical_weapon": 3,
    "legendary": 4,
    "legendary_weapon": 4,
    "ancient": 5,
    "ancient_weapon": 5,
    "contraband": 6,
    "extraordinary": 6,
}
_STEAM_RARITY_QUALITY_CN: dict[str, str] = {
    "common": "消费级",
    "uncommon": "工业级",
    "rare": "军规级",
    "mythical": "受限",
    "legendary": "保密",
    "ancient": "隐秘",
    "contraband": "非凡",
    "extraordinary": "非凡",
}


def _normalized_inventory_rarity(item: dict) -> str:
    rarity = str(item.get("rarity") or "").strip().lower()
    if rarity.startswith("rarity_"):
        rarity = rarity[len("rarity_") :]
    if rarity.endswith("_weapon"):
        rarity = rarity[: -len("_weapon")].strip("_")
    return rarity


def _inventory_item_is_star(item: dict) -> bool:
    """手套 / 刀具等 ★ 物品。Steam 常标 ancient，与隐秘枪同档，展示应为非凡。"""
    for key in ("market_name", "market_hash_name", "name"):
        if "★" in str(item.get(key) or ""):
            return True
    return False


def _inventory_item_quality_cn(item: dict) -> str:
    """Fast Steam-tag quality; metadata lookup is only a fallback."""
    rarity = _normalized_inventory_rarity(item)
    # Steam 将手套/刀具标为 ancient（与 Covert 枪同 internal），游戏内为非凡。
    if _inventory_item_is_star(item) and rarity in {"", "ancient"}:
        return "非凡"
    quality = _STEAM_RARITY_QUALITY_CN.get(rarity)
    if quality:
        return quality
    template = resolve_inventory_skin_template(item)
    return str(getattr(template, "quality", "") or "") if template else ""


def _inventory_item_quality_rank(item: dict) -> int:
    """品质排序键：优先使用 Steam 标签，缺失时再查询模板；未知靠后。"""
    rarity = _normalized_inventory_rarity(item)
    if _inventory_item_is_star(item) and rarity in {"", "ancient"}:
        return _QUALITY_RANK_CN["非凡"]
    if rarity in _STEAM_RARITY_RANK:
        return _STEAM_RARITY_RANK[rarity]
    template = resolve_inventory_skin_template(item)
    if template is not None:
        q = str(getattr(template, "quality", "") or "").strip()
        if q in _QUALITY_RANK_CN:
            return _QUALITY_RANK_CN[q]
    # 兼容 ``Rarity_Rare_Weapon`` 一类残留
    for key, rank in _STEAM_RARITY_RANK.items():
        if rarity.endswith(key) or key in rarity:
            return rank
    return 99


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _load_json_list(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    except Exception:
        return []


def _inventory_total_value(items: list[dict]) -> tuple[float, int]:
    total = 0.0
    matched = 0
    for item in items:
        try:
            price = float(item.get("buff_price"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            total += price
            matched += 1
    return total, matched


def _format_inventory_status_line(item: dict) -> str:
    if item.get("cooldown_kind") == "market_listed":
        return "Steam在售中"
    if item.get("marketable"):
        return "可出售"
    ends = item.get("cooldown_ends_at")
    if item.get("cooldown_kind") != "trade_hold" or ends is None:
        return "冷却中"
    try:
        remaining = max(0.0, float(ends) - time.time())
    except (TypeError, ValueError):
        return "冷却中"
    if remaining <= 0:
        return "可出售"
    if remaining >= 86400:
        days = int(remaining // 86400)
        hours = int((remaining % 86400) // 3600)
        return f"{days}天{hours}小时"
    if remaining >= 3600:
        return f"{int(remaining // 3600)}小时"
    return f"{max(1, int(remaining // 60))}分钟"


def _inventory_status_category(item: dict) -> str:
    """Stable filter bucket; card text may contain a live remaining duration."""
    if item.get("cooldown_kind") == "market_listed":
        return "Steam在售中"
    if item.get("marketable"):
        return "可出售"
    if item.get("cooldown_kind") == "trade_hold":
        ends = item.get("cooldown_ends_at")
        try:
            if ends is not None and float(ends) <= time.time():
                return "可出售"
        except (TypeError, ValueError):
            pass
    return "冷却中"


def _inventory_item_display_name(item: dict) -> str:
    return str(
        item.get("market_name")
        or item.get("name")
        or item.get("market_hash_name")
        or "未知饰品"
    )


def _blend_color(base: QColor, accent: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(base.red() * (1 - ratio) + accent.red() * ratio),
        round(base.green() * (1 - ratio) + accent.green() * ratio),
        round(base.blue() * (1 - ratio) + accent.blue() * ratio),
    )


class InventoryCardDelegate(QStyledItemDelegate):
    """Paint inventory cards without allocating a QWidget tree per asset."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(_INV_GRID_W, _INV_GRID_H)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        app = QApplication.instance()
        palette = app.palette() if app is not None else option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        card = QRectF(option.rect.adjusted(6, 6, -6, -6))
        card_bg = palette.color(QPalette.ColorRole.AlternateBase)
        border = palette.color(QPalette.ColorRole.Mid)
        accent = palette.color(QPalette.ColorRole.Highlight)
        if selected:
            card_bg = _blend_color(card_bg, accent, 0.12)
            border = accent
        painter.setPen(QPen(border, 2 if selected else 1))
        painter.setBrush(card_bg)
        painter.drawRoundedRect(card, 11, 11)

        quality = str(index.data(_ROLE_QUALITY) or "")
        line_hex, tint_hex = line_and_tint_for_quality_cn(quality)
        tint = QColor(tint_hex)
        image_rect = QRectF(
            card.left() + 8,
            card.top() + 8,
            _INV_WEAPON_ICON_W,
            _INV_IMAGE_AREA_H,
        )
        image_top = palette.color(QPalette.ColorRole.Window)
        gradient = QLinearGradient(image_rect.topLeft(), image_rect.bottomLeft())
        gradient.setColorAt(0.0, image_top)
        gradient.setColorAt(0.55, image_top)
        gradient.setColorAt(1.0, _blend_color(image_top, tint, 0.22))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(image_rect, 8, 8)
        painter.setBrush(QColor(line_hex))
        painter.drawRoundedRect(
            QRectF(image_rect.left(), image_rect.bottom() - 3, image_rect.width(), 3),
            1.5,
            1.5,
        )

        pixmap = index.data(_ROLE_ICON)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                _INV_WEAPON_ICON_W,
                _INV_WEAPON_ICON_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target_x = image_rect.left() + (image_rect.width() - scaled.width()) / 2
            target_y = image_rect.top() + (image_rect.height() - scaled.height()) / 2 - 1
            painter.drawPixmap(round(target_x), round(target_y), scaled)
        elif bool(index.data(_ROLE_ICON_LOADED)):
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(
                image_rect,
                Qt.AlignmentFlag.AlignCenter,
                "暂无图片",
            )

        text_color = palette.color(QPalette.ColorRole.Text)
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        name_rect = QRectF(card.left() + 8, image_rect.bottom() + 7, image_rect.width(), 34)
        name_font = painter.font()
        name_font.setPointSize(10)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(text_color)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            str(index.data(_ROLE_NAME) or ""),
        )

        meta_font = painter.font()
        meta_font.setPointSize(9)
        meta_font.setBold(False)
        painter.setFont(meta_font)
        painter.setPen(muted)
        wear_rect = QRectF(card.left() + 8, name_rect.bottom() + 3, image_rect.width(), 17)
        painter.drawText(
            wear_rect,
            Qt.AlignmentFlag.AlignCenter,
            str(index.data(_ROLE_WEAR) or ""),
        )
        price_rect = QRectF(card.left() + 8, wear_rect.bottom() + 1, image_rect.width(), 17)
        painter.setPen(accent)
        painter.drawText(
            price_rect,
            Qt.AlignmentFlag.AlignCenter,
            str(index.data(_ROLE_PRICE) or "￥-"),
        )
        status_rect = QRectF(card.left() + 8, price_rect.bottom() + 1, image_rect.width(), 17)
        painter.setPen(
            QColor("#21c997")
            if str(index.data(_ROLE_STATUS) or "") == "可出售"
            else muted
        )
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignCenter,
            str(index.data(_ROLE_STATUS) or ""),
        )
        painter.restore()


class InventoryPage(QWidget):
    import_to_alchemy_requested = Signal(object, object)
    import_to_simulation_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._filtered: list[dict] = []
        self._icon_cache: dict[str, QPixmap] = {}
        self._price_map: dict | None = None
        self._price_map_loaded = False
        self._login_worker: object | None = None
        self._fetch_worker: object | None = None
        self._list_build_generation = 0
        self._list_build_items: list[dict] = []
        self._list_build_next = 0
        self._icon_load_generation = 0
        self._pending_icon_indices: list[int] = []
        self._pending_icon_next = 0
        self._render_completion_status = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        root.setSpacing(16)
        root.addWidget(
            PageHeader(
                "Steam Vault",
                "Steam 库存管理",
                "会话仅保存在本机；网络与浏览器操作在后台线程执行。",
            )
        )

        controls_frame, controls = panel(self)
        account_row = QHBoxLayout()
        account_row.addWidget(QLabel("当前账号"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(180)
        self.add_button = QPushButton("登录 / 添加账号")
        self.add_button.setObjectName("primaryButton")
        self.fetch_button = QPushButton("获取库存")
        self.fetch_button.setEnabled(False)
        self.remove_button = QPushButton("移除账号")
        self.remove_button.setObjectName("dangerButton")
        self.remove_button.setEnabled(False)
        account_row.addWidget(self.account_combo)
        account_row.addWidget(self.add_button)
        account_row.addWidget(self.fetch_button)
        account_row.addWidget(self.remove_button)
        account_row.addStretch(1)
        controls.addLayout(account_row)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索饰品名称")
        self.search_edit.setClearButtonEnabled(True)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(
            ("全部品质", "消费级", "工业级", "军规级", "受限", "保密", "隐秘", "非凡")
        )
        self.status_combo = QComboBox()
        self.status_combo.addItems(("全部状态", "可出售", "冷却中", "Steam在售中"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(("默认排序", "品质低到高", "品质高到低"))
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setEnabled(False)
        self.import_calc_button = QPushButton("导入计算 ▾")
        self.import_calc_button.setEnabled(False)
        import_menu = QMenu(self.import_calc_button)
        merge_action = import_menu.addAction("追加到底物数据")
        replace_action = import_menu.addAction("替换底物数据")
        merge_action.triggered.connect(
            lambda _checked=False: self._emit_import_to_alchemy("merge")
        )
        replace_action.triggered.connect(
            lambda _checked=False: self._emit_import_to_alchemy("replace")
        )
        self.import_calc_button.setMenu(import_menu)
        self.import_sim_button = QPushButton("导入模拟")
        self.import_sim_button.setEnabled(False)
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.quality_combo)
        filter_row.addWidget(self.status_combo)
        filter_row.addWidget(self.sort_combo)
        filter_row.addWidget(self.select_all_button)
        filter_row.addWidget(self.import_calc_button)
        filter_row.addWidget(self.import_sim_button)
        controls.addLayout(filter_row)

        self.status_label = QLabel("本地无数据，请先登录 Steam")
        self.status_label.setObjectName("statusLabel")
        self.inventory_total_value_label = QLabel("库存总价值：￥0.00")
        self.inventory_total_value_label.setObjectName("inventoryTotalValue")
        self.inventory_total_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.inventory_total_value_label.setToolTip("按当前已匹配价格汇总全部库存饰品")
        summary_row = QGridLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.addWidget(
            self.status_label,
            0,
            0,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        summary_row.addWidget(
            self.inventory_total_value_label,
            0,
            1,
            Qt.AlignmentFlag.AlignCenter,
        )
        summary_row.setColumnStretch(0, 1)
        summary_row.setColumnStretch(1, 1)
        summary_row.setColumnStretch(2, 1)
        controls.addLayout(summary_row)
        root.addWidget(controls_frame)

        list_frame, list_layout = panel(self)
        self.inventory_list = QListWidget()
        self.inventory_list.setObjectName("inventoryList")
        self.inventory_list.setViewMode(QListView.ViewMode.IconMode)
        self.inventory_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.inventory_list.setMovement(QListView.Movement.Static)
        self.inventory_list.setWrapping(True)
        self.inventory_list.setUniformItemSizes(True)
        self.inventory_list.setSpacing(8)
        self.inventory_list.setGridSize(QSize(_INV_GRID_W, _INV_GRID_H))
        self.inventory_list.setItemDelegate(InventoryCardDelegate(self.inventory_list))
        self.inventory_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.inventory_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        list_layout.addWidget(self.inventory_list, 1)
        root.addWidget(list_frame, 1)

        self.account_combo.currentIndexChanged.connect(self._account_changed)
        self.add_button.clicked.connect(self._start_login)
        self.fetch_button.clicked.connect(self._start_fetch)
        self.remove_button.clicked.connect(self._remove_account)
        self.search_edit.textChanged.connect(self._apply_filters)
        self.quality_combo.currentIndexChanged.connect(self._apply_filters)
        self.status_combo.currentIndexChanged.connect(self._apply_filters)
        self.sort_combo.currentIndexChanged.connect(self._apply_filters)
        self.inventory_list.customContextMenuRequested.connect(self._open_context_menu)
        self.inventory_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.select_all_button.clicked.connect(self._toggle_select_all)
        self.import_sim_button.clicked.connect(self._emit_import_to_simulation)
        self.inventory_list.verticalScrollBar().valueChanged.connect(
            lambda _value: QTimer.singleShot(0, self._load_visible_icons)
        )

        # Paint the page shell first; local account/inventory hydration then
        # proceeds incrementally on the event loop.
        QTimer.singleShot(0, self._reload_accounts)

    def _active_profile_id(self) -> str:
        return str(self.account_combo.currentData() or "")

    def _set_busy(self, busy: bool) -> None:
        self.account_combo.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.fetch_button.setEnabled(not busy and bool(self._active_profile_id()))
        self.remove_button.setEnabled(not busy and bool(self._active_profile_id()))

    def _reload_accounts(self, preferred: str = "") -> None:
        active = preferred or get_active_profile_id()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        selected = -1
        for index, entry in enumerate(list_profile_entries()):
            profile_id = str(entry.get("id") or "")
            self.account_combo.addItem(combo_display_name_for_profile(entry), profile_id)
            if profile_id == active:
                selected = index
        if selected >= 0:
            self.account_combo.setCurrentIndex(selected)
        self.account_combo.blockSignals(False)
        has_account = self.account_combo.count() > 0
        self.fetch_button.setEnabled(has_account)
        self.remove_button.setEnabled(has_account)
        self._load_cached_inventory()

    def _account_changed(self) -> None:
        profile_id = self._active_profile_id()
        if profile_id:
            set_active_profile(profile_id)
        self._load_cached_inventory()

    def _load_cached_inventory(self) -> None:
        profile_id = self._active_profile_id()
        self._items = _load_json_list(profile_inventory_data_path(profile_id)) if profile_id else []
        self._update_inventory_total_value()
        self._apply_filters()
        if profile_id:
            self.status_label.setText(
                f"已加载本地库存 {len(self._items)} 件"
                if self._items
                else "当前账号暂无本地库存，请点击“获取库存”"
            )
        else:
            self.status_label.setText("尚未添加 Steam 账号")

    def _update_inventory_total_value(self) -> None:
        total, matched = _inventory_total_value(self._items)
        self.inventory_total_value_label.setText(f"库存总价值：￥{total:,.2f}")
        self.inventory_total_value_label.setToolTip(
            f"全部库存共 {len(self._items)} 件，其中 {matched} 件已匹配价格"
        )

    def _start_login(self) -> None:
        if self._login_worker and self._login_worker.isRunning():
            return
        from ui.workers.steam import SteamLoginWorker

        root = prepare_pending_add_account_root()
        self._set_busy(True)
        self.status_label.setText("正在打开浏览器…")
        self._login_worker = SteamLoginWorker(root, self)
        self._login_worker.status.connect(self.status_label.setText)
        self._login_worker.completed.connect(self._login_finished)
        self._login_worker.start()

    def _login_finished(self, profile: dict | None, error: str) -> None:
        self._set_busy(False)
        if error or not profile:
            discard_pending_add_account_root()
            self.status_label.setText(error or "Steam 登录未完成")
            return
        try:
            profile_id = commit_pending_steam_profile(
                pending_add_account_root(),
                steam_id=str(profile.get("steam_id") or ""),
                personaname=str(profile.get("personaname") or ""),
                avatar_path=str(profile.get("avatar_path") or ""),
                avatar_url=str(profile.get("avatar_url") or ""),
            )
        except Exception as exc:
            discard_pending_add_account_root()
            self.status_label.setText(str(exc))
            return
        self._reload_accounts(profile_id)
        self.status_label.setText(f"已保存 Steam 账号：{profile.get('personaname') or 'Steam'}")

    def _start_fetch(self) -> None:
        profile_id = self._active_profile_id()
        if not profile_id or (self._fetch_worker and self._fetch_worker.isRunning()):
            return
        from ui.workers.steam import InventoryFetchWorker

        cfg = load_steam_account_config_dict(profile_id)
        self._set_busy(True)
        self.status_label.setText("正在获取库存…")
        self._fetch_worker = InventoryFetchWorker(
            profile_session_root(profile_id),
            str(cfg.get("steam_id") or ""),
            self,
        )
        self._fetch_worker.status.connect(self.status_label.setText)
        self._fetch_worker.completed.connect(
            lambda items, profile, error, price_status, pid=profile_id: self._fetch_finished(
                pid, items, profile, error, price_status
            )
        )
        self._fetch_worker.start()

    def _fetch_finished(
        self,
        profile_id: str,
        items: list[dict] | None,
        profile: dict | None,
        error: str,
        price_status: str,
    ) -> None:
        self._set_busy(False)
        if error:
            self.status_label.setText(error)
            return
        if items is None:
            return
        _atomic_json_write(profile_inventory_data_path(profile_id), items)
        # The worker has just refreshed the on-disk package.  Invalidate the
        # page-level map so later imports use the same prices shown on cards.
        self._price_map = None
        self._price_map_loaded = False
        self._render_completion_status = price_status or f"库存更新完成，共 {len(items)} 件"
        if profile:
            save_steam_account_config_dict(
                profile_id,
                {
                    "steam_id": str(profile.get("steam_id") or ""),
                    "steam_personaname": str(profile.get("personaname") or ""),
                    "steam_avatar_path": str(profile.get("avatar_path") or ""),
                    "steam_avatar_url": str(profile.get("avatar_url") or ""),
                },
            )
            update_profile_display_name(profile_id, str(profile.get("personaname") or "Steam"))
        self._reload_accounts(profile_id)
        try:
            reconciliation = reconcile_all_purchase_records_for_profile(profile_id, items)
        except Exception as exc:
            self._render_completion_status += f"；配方采购核对失败：{exc}"
            self.status_label.setText(self._render_completion_status)
            return
        matched = int(reconciliation.get("matched") or 0)
        waiting = int(reconciliation.get("waiting") or 0)
        save_failures = int(reconciliation.get("save_failures") or 0)
        if matched:
            self._render_completion_status += f"；配方采购新入库 {matched} 件"
        if waiting:
            self._render_completion_status += f"，仍待入库 {waiting} 件"
        if save_failures:
            self._render_completion_status += f"；{save_failures} 个配方状态保存失败"
        self.status_label.setText(self._render_completion_status)

    def _remove_account(self) -> None:
        profile_id = self._active_profile_id()
        if not profile_id:
            return
        name = self.account_combo.currentText()
        if not ask_confirmation(
            self,
            "移除 Steam 账号",
            f"确定移除“{name}”及其本地登录态和库存缓存吗？",
        ):
            return
        next_profile = delete_steam_profile(profile_id)
        self._reload_accounts(next_profile)
        self.status_label.setText("账号及本地会话已移除")

    @staticmethod
    def _item_name(item: dict) -> str:
        return _inventory_item_display_name(item)

    def _apply_filters(self) -> None:
        query = self.search_edit.text().strip().casefold()
        quality = self.quality_combo.currentText()
        status = self.status_combo.currentText()
        sort_mode = self.sort_combo.currentText()
        filtered: list[dict] = []
        for item in self._items:
            if query and query not in self._item_name(item).casefold():
                continue
            if quality != "全部品质":
                if _inventory_item_quality_cn(item) != quality:
                    continue
            current_status = _inventory_status_category(item)
            if status != "全部状态" and current_status != status:
                continue
            filtered.append(item)
        if sort_mode == "品质低到高":
            filtered.sort(key=lambda it: (_inventory_item_quality_rank(it), self._item_name(it)))
        elif sort_mode == "品质高到低":
            filtered.sort(
                key=lambda it: (-_inventory_item_quality_rank(it), self._item_name(it))
            )
        self._filtered = filtered
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        self._list_build_generation += 1
        self._icon_load_generation += 1
        generation = self._list_build_generation
        self._list_build_items = list(self._filtered)
        self._list_build_next = 0
        self.inventory_list.clear()
        self._sync_import_buttons()
        QTimer.singleShot(
            1,
            lambda current=generation: self._append_inventory_list_batch(current),
        )

    def _append_inventory_list_batch(self, generation: int) -> None:
        """Incrementally materialize inventory cards without blocking page switches."""
        if generation != self._list_build_generation:
            return
        start = self._list_build_next
        total = len(self._list_build_items)
        batch_size = 2 if start == 0 else _INV_RENDER_BATCH_SIZE
        end = min(total, start + batch_size)
        hint = QSize(_INV_GRID_W, _INV_GRID_H)
        from core.alchemy_calc import format_inventory_yuan_price

        self.inventory_list.setUpdatesEnabled(False)
        try:
            for index in range(start, end):
                item = self._list_build_items[index]
                quality_cn = _inventory_item_quality_cn(item)
                name = _inventory_item_display_name(item)
                wear = inventory_wear_chinese(item) or "未知磨损"
                raw_float = item.get("float")
                try:
                    float_text = (
                        format_float_shortest(float(raw_float))
                        if raw_float is not None
                        else "—"
                    )
                    tip_float = (
                        f"{float(raw_float):.10f}"
                        if raw_float is not None
                        else "—"
                    )
                except (TypeError, ValueError):
                    float_text = "—"
                    tip_float = "—"
                status = _format_inventory_status_line(item)
                raw_price = item.get("buff_price")
                try:
                    price_value = float(raw_price) if raw_price is not None else None
                except (TypeError, ValueError):
                    price_value = None
                price_text = format_inventory_yuan_price(price_value)
                list_item = QListWidgetItem()
                list_item.setData(Qt.ItemDataRole.UserRole, index)
                list_item.setData(_ROLE_NAME, name)
                list_item.setData(_ROLE_WEAR, f"{wear} · {float_text}")
                list_item.setData(_ROLE_STATUS, status)
                list_item.setData(_ROLE_QUALITY, quality_cn)
                list_item.setData(_ROLE_ICON_LOADED, False)
                list_item.setData(_ROLE_PRICE, price_text)
                list_item.setToolTip(
                    f"{name}\n磨损：{tip_float}\n参考价："
                    f"{format_inventory_yuan_price(price_value)}\n{status}"
                )
                list_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                list_item.setSizeHint(hint)
                self.inventory_list.addItem(list_item)
        finally:
            self.inventory_list.setUpdatesEnabled(True)

        self._list_build_next = end
        if end < total:
            self.status_label.setText(
                f"正在渲染库存 {end} / {total} 件…"
            )
            QTimer.singleShot(
                16,
                lambda current=generation: self._append_inventory_list_batch(current),
            )
            return

        self._on_selection_changed()
        if self._render_completion_status:
            self.status_label.setText(self._render_completion_status)
            self._render_completion_status = ""
        elif self._items:
            self.status_label.setText(f"显示 {len(self._filtered)} / {len(self._items)} 件")
        # Let the fully painted card wall reach screen before resolving the
        # heavier local skin-image index for the first time.
        QTimer.singleShot(80, self._load_visible_icons)

    def _on_selection_changed(self) -> None:
        self._sync_import_buttons()

    def _toggle_select_all(self) -> None:
        total = self.inventory_list.count()
        if total <= 0:
            return
        if len(self.inventory_list.selectedItems()) == total:
            self.inventory_list.clearSelection()
        else:
            self.inventory_list.selectAll()

    def selected_inventory_items(self) -> list[dict]:
        selected: list[dict] = []
        for widget_item in self.inventory_list.selectedItems():
            index = widget_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(index, int) and 0 <= index < len(self._filtered):
                selected.append(self._filtered[index])
        return selected

    def _sync_import_buttons(self) -> None:
        selected_count = len(self.inventory_list.selectedItems())
        total = self.inventory_list.count()
        enabled = selected_count > 0
        self.import_calc_button.setEnabled(enabled)
        self.import_sim_button.setEnabled(enabled)
        self.select_all_button.setEnabled(total > 0)
        self.select_all_button.setText(
            "取消全选" if total > 0 and selected_count == total else "全选"
        )

    def _emit_import_to_alchemy(self, mode: str) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            return
        mapped = [row for item in selected if (row := self._to_alchemy_item(item))]
        if not mapped:
            self.status_label.setText("所选物品缺少可识别的皮肤信息或磨损值，无法导入")
            return
        self.import_to_alchemy_requested.emit(mapped, mode)

    def _load_price_map_once(self) -> dict | None:
        if not self._price_map_loaded:
            from core.alchemy_calc import try_build_product_price_map_from_disk

            self._price_map_loaded = True
            self._price_map = try_build_product_price_map_from_disk()
        return self._price_map

    def _to_alchemy_item(self, item: dict) -> dict | None:
        template = resolve_inventory_skin_template(item)
        if template is None:
            return None
        weapon = str(getattr(template, "weapon_name", "") or "").strip()
        skin = str(getattr(template, "skin_name", "") or "").strip()
        if not weapon:
            return None
        goods_name = f"{weapon} | {skin}" if skin else weapon
        wear = inventory_wear_chinese(item)
        if wear:
            goods_name = f"{goods_name}（{wear}）"
        raw_float = item.get("float", item.get("float_value"))
        try:
            float_value = float(raw_float)
        except (TypeError, ValueError):
            return None
        from core.alchemy_calc import lookup_inventory_item_price_value

        try:
            stored_price = float(item.get("buff_price"))
        except (TypeError, ValueError):
            stored_price = 0.0
        price = (
            stored_price
            if stored_price > 0
            else lookup_inventory_item_price_value(item, self._load_price_map_once())
        )
        return {
            "float_value": float_value,
            "goods_id": str(item.get("assetid") or goods_name),
            "goods_name": goods_name,
            "platform": "inventory",
            "price": float(price or 0.0),
        }

    def _emit_import_to_simulation(self) -> None:
        items = self.selected_inventory_items()
        if items:
            self.import_to_simulation_requested.emit(items)

    def clear_selected_after_successful_import(self) -> None:
        self.inventory_list.clearSelection()
        self._sync_import_buttons()

    def _visible_indices(self) -> set[int]:
        viewport = self.inventory_list.viewport()
        width = max(1, viewport.width())
        height = max(1, viewport.height())
        points = []
        for y in (4, height // 2, max(4, height - 4)):
            for x in range(4, width, max(40, self.inventory_list.gridSize().width() // 2)):
                points.append(QPoint(x, y))
        indices = {
            index.row()
            for point in points
            if (index := self.inventory_list.indexAt(point)).isValid()
        }
        if not indices and self.inventory_list.count():
            indices.add(0)
        if indices:
            low = max(0, min(indices) - 6)
            high = min(self.inventory_list.count(), max(indices) + 7)
            indices.update(range(low, high))
        return indices

    def _load_visible_icons(self) -> None:
        self._icon_load_generation += 1
        generation = self._icon_load_generation
        self._pending_icon_indices = sorted(self._visible_indices())
        self._pending_icon_next = 0
        self._load_visible_icon_batch(generation)

    def _load_visible_icon_batch(self, generation: int) -> None:
        """Load a small image batch per event turn to keep scrolling responsive."""
        if generation != self._icon_load_generation:
            return
        start = self._pending_icon_next
        end = min(len(self._pending_icon_indices), start + 6)
        for pending_index in range(start, end):
            list_index = self._pending_icon_indices[pending_index]
            widget_item = self.inventory_list.item(list_index)
            if widget_item is None or bool(widget_item.data(_ROLE_ICON_LOADED)):
                continue
            filtered_index = widget_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(filtered_index, int) or filtered_index >= len(self._filtered):
                continue
            path = resolve_inventory_item_icon_path(self._filtered[filtered_index])
            if not path:
                widget_item.setData(_ROLE_ICON_LOADED, True)
                continue
            pixmap = self._icon_cache.get(path)
            if pixmap is None:
                loaded = QPixmap(path)
                if loaded.isNull():
                    widget_item.setData(_ROLE_ICON_LOADED, True)
                    continue
                self._icon_cache[path] = loaded
                pixmap = loaded
            widget_item.setData(_ROLE_ICON, pixmap)
            widget_item.setData(_ROLE_ICON_LOADED, True)
            self.inventory_list.viewport().update(
                self.inventory_list.visualItemRect(widget_item)
            )
        self._pending_icon_next = end
        if end < len(self._pending_icon_indices):
            QTimer.singleShot(
                8,
                lambda current=generation: self._load_visible_icon_batch(current),
            )

    def _item_at(self, point: QPoint) -> dict | None:
        widget_item = self.inventory_list.itemAt(point)
        if widget_item is None:
            return None
        index = widget_item.data(Qt.ItemDataRole.UserRole)
        return self._filtered[index] if isinstance(index, int) and index < len(self._filtered) else None

    def _open_context_menu(self, point: QPoint) -> None:
        widget_item = self.inventory_list.itemAt(point)
        if widget_item is not None and not widget_item.isSelected():
            self.inventory_list.clearSelection()
            widget_item.setSelected(True)
            self.inventory_list.setCurrentItem(widget_item)
        item = self._item_at(point)
        if item is None:
            return
        template = resolve_inventory_skin_template(item)
        if template is None:
            self.status_label.setText("该饰品未匹配到本地模板，无法生成平台直达链接")
            return
        wear = APPEARANCE_MAP.get(str(item.get("wear") or ""), str(item.get("wear") or ""))
        links = links_for_template(template, wear, max_wear=item.get("float"))
        menu = QMenu(self)
        for marketplace in MARKETPLACES:
            action = QAction(f"在 {marketplace.name} 查看", menu)
            action.triggered.connect(
                lambda _=False, url=links[marketplace.key]: QDesktopServices.openUrl(QUrl(url))
            )
            menu.addAction(action)
        menu.exec(self.inventory_list.viewport().mapToGlobal(point))

    def close_workers(self) -> None:
        # Invalidate deferred UI batches before stopping network workers.  This
        # keeps queued timers from touching card widgets during application exit.
        self._list_build_generation += 1
        self._icon_load_generation += 1
        for worker in (self._login_worker, self._fetch_worker):
            if worker and worker.isRunning():
                worker.requestInterruption()
                worker.wait(1500)
