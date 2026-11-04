"""ECO 合炉创建、材料上架、查询及本地 API 配置。"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget, QSizePolicy,
    QFrame, QListWidget, QListWidgetItem, QMessageBox, QStyle,
)

from core.eco_furnace import (
    EcoApiError, EcoCredentials, EcoFurnaceClient, FURNACE_STATES,
    build_create_payload, generate_key_pair, load_eco_credentials, match_recipe_inventory,
    public_key_for, save_eco_credentials,
)
from core.inventory_steam_accounts import list_profile_entries, load_steam_account_config_dict
from core.app_settings_store import load_app_settings, update_app_settings
from core.material_market_prices import material_market_prices
from core.eco_publication import furnace_is_published, sync_furnace_publications
from core.purchase_tracking import load_profile_inventory_items
from ui.dialog_topmost_support import apply_frameless_modal_geometry, install_dialog_topmost_follow_parent
from ui.feedback import ask_confirmation
from ui.modal_shell import build_frameless_modal_content, wire_overlay_dismiss


def _furnace_reference_text(row):
    try:
        value = Decimal(str(row.get("AllMatRefPrice")))
        if value.is_finite() and value >= 0:
            return f"¥{value:.2f}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return "未返回"


class _EcoRequestWorker(QThread):
    progress = Signal(str)
    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = ""
        self.uncertain = False

    def run(self):
        try:
            self.result = self.operation()
        except EcoApiError as exc:
            self.error, self.uncertain = str(exc), exc.uncertain
        except Exception:
            logging.getLogger(__name__).exception("ECO request worker failed")
            self.error = "ECO 操作未完成，请检查 API 设置和网络后重试"
            self.uncertain = True


class EcoFurnaceDialog(QDialog):
    publication_changed = Signal(str, object, str)
    PRICE_COLUMN = 3
    def __init__(self, recipe: dict, parent=None, *, profile_id="", title="", initial_tab="publish", furnace_id="", resume=False, listing_state=None):
        super().__init__(parent)
        dialog_title = {"settings": "ECO合炉 · API 设置", "list": "ECO合炉 · 我的合炉"}.get(initial_tab, "发布 ECO 合炉")
        self.setWindowTitle(dialog_title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.recipe = copy.deepcopy(recipe)
        purchase_prices = [row.get("price") for row in self.recipe.get("substrates_display") or []]
        self._purchase_cost = sum((Decimal(str(value)) for value in purchase_prices), Decimal("0")) if purchase_prices and all(value is not None for value in purchase_prices) else None
        self._worker = None
        self._operation = ""
        self._create_locked = resume
        self._create_payload = None
        self._assets = []
        self._listed = []
        self._created_furnace_id = furnace_id
        self._pre_create_furnace_ids = set()
        self._awaiting_new_furnace = False
        self._resume = resume
        self._workflow_auto_started = False
        self._workflow_furnace = {}
        self._published_asset_ids = set((listing_state or {}).get("listed_assets") or [])
        self._listing_uncertain = bool((listing_state or {}).get("uncertain"))
        self._market_prices = {}
        self._listing_prices = dict((listing_state or {}).get("prices") or {})
        self._controls = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, self.box, layout, close = build_frameless_modal_content(self, dialog_title, box_width=960, include_message=False)
        root.addWidget(overlay)
        close.clicked.connect(self.reject)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._publish_tab = QWidget()
        self._list_tab = QWidget()
        self._settings_tab = QWidget()
        self.tabs.addTab(self._publish_tab, "发布合炉")
        self.tabs.addTab(self._list_tab, "我的合炉")
        self.tabs.addTab(self._settings_tab, "API 设置")
        self._build_publish_tab(profile_id, title)
        self._build_list_tab()
        self._build_settings_tab()
        self.status_label = QLabel("直接套用已有合炉读取参考价值，或按原流程新建；材料定价和上架在本页完成。")
        self.status_label.setObjectName("alchemyStep1Hint")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.PlainText)
        self._set_status(self.status_label.text())
        layout.addWidget(self.status_label)
        self._match_inventory()
        self._created_furnace_id = furnace_id
        self._sync_publish_view()
        if not self.recipe.get("substrates_display"):
            self.tabs.setTabVisible(0, False)
        if initial_tab == "settings":
            self.tabs.setCurrentWidget(self._settings_tab)
        elif initial_tab == "list" or not self.recipe.get("substrates_display"):
            self.tabs.setCurrentWidget(self._list_tab)
        if initial_tab in {"settings", "list"}:
            self._set_status("配置 ECO 开放平台授权后，可查询合炉；配方发布统一从采购管理的 ECO合炉 页面进入。")
        wire_overlay_dismiss(overlay, self.box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

    def _set_status(self, message, *, error=False):
        if hasattr(self, "create_button") and not self._workflow_furnace and not self._create_locked:
            reason = self._create_block_reason()
            if reason and self._worker is None and not error:
                message, error = reason, True
        self.status_label.setText(message)
        color = "#ef4444" if error else "#16a34a"
        self.status_label.setStyleSheet(f"QLabel {{ color: {color}; }}")

    def _button(self, text, action, layout):
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setAutoDefault(False)
        button.clicked.connect(action)
        layout.addWidget(button)
        self._controls.append(button)
        return button

    def _sync_publish_view(self):
        self._publish_form_widget.setEnabled(not self._create_locked)
        self._sync_buttons()

    def _refresh_workflow_furnaces(self):
        self.state_combo.setCurrentIndex(self.state_combo.findData(100))
        self.page_spin.setValue(1)
        self._query()

    def _populate_workflow_furnaces(self):
        combo = self.workflow_furnace_combo
        selected = self._created_furnace_id
        candidates = self._listed
        if not selected and self._awaiting_new_furnace:
            new = [row for row in candidates if str(row.get("FurnaceID") or "") not in self._pre_create_furnace_ids and row.get("Title") == self._create_payload["Title"]]
            if len(new) == 1:
                selected = str(new[0]["FurnaceID"])
                self._awaiting_new_furnace = False
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("新建合炉 / 请选择要套用的已有合炉", None)
        for row in candidates:
            reference_text = _furnace_reference_text(row)
            current = " · 当前配方" if str(row.get("FurnaceID")) == selected else ""
            combo.addItem(f"{row.get('Title') or ''} · 参考 {reference_text} · 合炉已添加 {row.get('AddMatCount', '未返回')} 件{current}", row)
            combo.setItemData(combo.count() - 1, str(row.get("FurnaceID") or ""), Qt.ToolTipRole)
        index = next((i + 1 for i, row in enumerate(candidates) if str(row.get("FurnaceID")) == selected), 0)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._select_workflow_furnace(index)

    def _select_workflow_furnace(self, _index):
        self._workflow_furnace = self.workflow_furnace_combo.currentData() or {}
        row = self._workflow_furnace
        if row:
            self._created_furnace_id = str(row.get("FurnaceID") or "")
            self.title_edit.setText(str(row.get("Title") or self.title_edit.text()))
            self._awaiting_new_furnace = False
            self._create_locked = True
            value = _furnace_reference_text(row)
            self.reference_total_label.setText(f"材料总参考价值：{value} · 已添加材料：{row.get('AddMatCount', '未返回')} · {'可支付押金' if row.get('CanPayment') is True else '暂不可支付押金'}")
            if self._create_payload is None:
                self._create_payload = {"ReceiveSteamID": self._profiles.get(str(self.account_combo.currentData()), ""), "AssetList": copy.deepcopy(self._assets)}
            self._save_listing_progress()
        else:
            self.reference_total_label.setText("材料总参考价值：创建或套用合炉后读取")
        self._sync_publish_view()

    def _publish_materials(self):
        if self._worker is not None or self._listing_uncertain or not self._workflow_furnace:
            return
        try:
            client = EcoFurnaceClient(self._credentials())
            assets = [dict(row, Price=self.material_table.cellWidget(index, self.PRICE_COLUMN).value()) for index, row in enumerate(self._assets) if row["AssetId"] not in self._published_asset_ids]
            if not assets:
                return
            for row in assets:
                if not row.get("AssetId") or row["Price"] <= 0:
                    raise EcoApiError("请先匹配材料，并为每件材料填写有效售价")
            steam_id = self._profiles.get(str(self.account_combo.currentData() or ""), "")
        except (ValueError, OSError) as exc:
            self._set_status(str(exc), error=True)
            return
        if not ask_confirmation(self, "确认材料售价并上架", f"账号：{self.account_combo.currentText()}\n当前合炉：{self._workflow_furnace.get('Title') or ''}\n{len(assets)} 件材料 · 私密上架总售价 ¥{sum(row['Price'] for row in assets):.2f}\n已私密上架的材料更新售价，未上架的材料按以上售价私密上架。完成后核对实际售价。"):
            return
        self._listing_assets = assets
        self._listing_uncertain = True
        self._save_listing_progress()
        self._start_request("publish_materials", lambda: client.publish_private_materials(steam_id, assets))

    def _save_listing_progress(self):
        if self._create_payload is not None:
            self._listing_prices = {row["AssetId"]: self.material_table.cellWidget(index, self.PRICE_COLUMN).value() for index, row in enumerate(self._assets)}
            self._create_payload["_material_listing"] = {"listed_assets": sorted(self._published_asset_ids), "uncertain": self._listing_uncertain, "prices": self._listing_prices}
            self.publication_changed.emit("submitted", self._create_payload, self._created_furnace_id)

    def _check_stock_status(self):
        if self._worker is not None:
            return
        try:
            client = EcoFurnaceClient(self._credentials())
            steam_id = self._profiles.get(str(self.account_combo.currentData() or ""), "")
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        self._start_request("stock_status", lambda: {"stock": client.stock_items(steam_id, [row["AssetId"] for row in self._assets]), "listed": client.private_listings(steam_id)})

    def _build_publish_tab(self, profile_id, title):
        layout = QVBoxLayout(self._publish_tab)
        layout.setAlignment(Qt.AlignTop)
        layout.setSpacing(8)
        self._step_label = QLabel("① 创建合炉  →  ② 确认最终售价")
        self._step_label.setObjectName("recipeSavedTitle")
        layout.addWidget(self._step_label)
        self._publish_form_widget = QWidget()
        self._publish_form_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form = QFormLayout(self._publish_form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(6)
        self.account_combo = QComboBox()
        self._profiles = {}
        for entry in list_profile_entries():
            profile = str(entry.get("id") or "")
            cfg = load_steam_account_config_dict(profile)
            if profile and cfg.get("steam_id"):
                self.account_combo.addItem(str(entry.get("display_name") or cfg.get("steam_personaname") or "未命名账号"), profile)
                self._profiles[profile] = cfg["steam_id"]
        inferred = next((str(row.get("steam_profile_id")) for row in self.recipe.get("substrates_display") or [] if row.get("steam_profile_id")), "")
        index = self.account_combo.findData(profile_id or inferred)
        if index >= 0:
            self.account_combo.setCurrentIndex(index)
        form.addRow("材料所属账号", self.account_combo)
        self.title_combo = QComboBox()
        self.title_combo.setEditable(True)
        self.title_combo.setInsertPolicy(QComboBox.NoInsert)
        self.title_combo.addItems(load_app_settings().get("eco_recent_furnace_titles", [])[:5])
        self.title_combo.setCurrentIndex(-1)
        self.title_edit = self.title_combo.lineEdit()
        self.title_edit.setPlaceholderText("请输入合炉标题，或选择最近使用的标题")
        self.title_edit.editingFinished.connect(self._remember_furnace_title)
        self.title_edit.textChanged.connect(lambda: self._sync_buttons() if hasattr(self, "create_button") else None)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_combo, 1)
        self.title_history_button = QPushButton()
        self.title_history_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowDown))
        self.title_history_button.setToolTip("选择最近使用的标题（最多5条）")
        self.title_history_button.setFixedWidth(32)
        self.title_history_button.setAutoDefault(False)
        self.title_history_button.clicked.connect(self.title_combo.showPopup)
        title_row.addWidget(self.title_history_button)
        form.addRow("合炉标题", title_row)
        options = QHBoxLayout()
        self.publish_combo = QComboBox()
        self.publish_combo.addItem("私密合炉", 201)
        self.publish_combo.addItem("公开合炉", 101)
        self.publish_combo.setCurrentIndex(self.publish_combo.findData(101))
        options.addWidget(self.publish_combo)
        options.addWidget(QLabel("开炉费"))
        self.fee_spin = QDoubleSpinBox()
        self.fee_spin.setRange(0, 10)
        self.fee_spin.setDecimals(2)
        self.fee_spin.setValue(10)
        self.fee_spin.setSuffix("%")
        options.addWidget(self.fee_spin)
        self.free_fee_check = QCheckBox("炸炉免开炉费")
        self.free_fee_check.setChecked(True)
        options.addWidget(self.free_fee_check)
        self.self_open_check = QCheckBox("人齐自开")
        self.self_open_check.setChecked(True)
        self.self_open_check.setToolTip("开启后，材料集齐时 ECO 会自动进行汰换并提交结果")
        options.addWidget(self.self_open_check)
        form.addRow("发布选项", options)
        layout.addWidget(self._publish_form_widget)
        self._workflow_target = QWidget()
        target_layout = QHBoxLayout(self._workflow_target)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.addWidget(QLabel("套用已有合炉"))
        self.workflow_furnace_combo = QComboBox()
        self.workflow_furnace_combo.addItem("新建合炉 / 请选择要套用的已有合炉", None)
        self.workflow_furnace_combo.currentIndexChanged.connect(self._select_workflow_furnace)
        target_layout.addWidget(self.workflow_furnace_combo, 1)
        refresh = self._button("刷新已有合炉", self._refresh_workflow_furnaces, target_layout)
        refresh.setToolTip("查询已有合炉供套用，并更新 ECO 合炉材料总参考价值；不查询材料市场价格。")
        layout.addWidget(self._workflow_target)
        self.material_table = QTableWidget(0, 4)
        self.material_table.setHorizontalHeaderLabels(["材料", "实际磨损", "Steam 资产 ID", "炉主上架售价"])
        self.material_table.setWordWrap(False)
        self.material_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.material_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.material_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.material_table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.material_table.verticalHeader().setDefaultSectionSize(32)
        self.material_table.horizontalHeader().setFixedHeight(36)
        self.material_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 180), (2, 180), (3, 125)):
            self.material_table.setColumnWidth(col, width)
        layout.addWidget(self.material_table)
        totals = QHBoxLayout()

        self.market_total_label = QLabel("ECO / BUFF同磨损区间参考价合计：未读取（不使用收料成本）")
        self.market_total_label.hide()
        self.reference_total_label = QLabel("材料总参考价值：创建后自动读取")
        self.reference_total_label.setToolTip("仅使用 ECO 合炉列表返回的 AllMatRefPrice；创建前暂不可获取。")
        totals.addWidget(self.reference_total_label, 1)
        self.purchase_cost_label = QLabel(f"收料成本：¥{self._purchase_cost:.2f}" if self._purchase_cost is not None else "收料成本：未记录")
        totals.addWidget(self.purchase_cost_label)
        self.sale_total_label = QLabel("炉主上架材料总售价：¥0.00")
        totals.addWidget(self.sale_total_label)
        layout.addLayout(totals)
        self._markup_widget = QWidget()
        markup = QHBoxLayout(self._markup_widget)
        markup.setContentsMargins(0, 0, 0, 0)
        markup.addWidget(QLabel("材料溢价幅度"))
        self.markup_spin = QDoubleSpinBox()
        self.markup_spin.setDecimals(1)
        self.markup_spin.setRange(-99, 10000)
        self.markup_spin.setValue(0)
        self.markup_spin.setSuffix(" %")
        self.markup_spin.setSingleStep(1.0)
        markup.addWidget(self.markup_spin)
        for icon, direction in ((QStyle.SP_ArrowUp, 1), (QStyle.SP_ArrowDown, -1)):
            step_button = QPushButton(self._markup_widget)
            step_button.setIcon(self.style().standardIcon(icon))
            step_button.setToolTip("增加 1%" if direction > 0 else "减少 1%")
            step_button.setFixedWidth(28)
            step_button.setAutoDefault(False)
            step_button.clicked.connect(lambda _checked=False, step=direction: self.markup_spin.stepBy(step))
            markup.addWidget(step_button)
        self.markup_button = QPushButton(self._markup_widget)
        self.markup_button.hide()
        self.markup_spin.valueChanged.connect(self._increase_prices)
        self.markup_button.setToolTip("每件 ECO / BUFF同磨损区间参考价乘以此比例；110% 即上涨10%，重复点击不累加。")
        markup.addStretch(1)

        self._workflow_hint = QLabel("套用已有合炉即可读取 ECO 材料总参考价值，无需重新创建；核对售价后可私密上架材料。")
        self._workflow_hint.setWordWrap(True)
        self._workflow_hint.hide()
        actions = QHBoxLayout()
        self._button("重新匹配库存", self._match_inventory, actions)
        self.stock_check_button = self._button("核对材料上架状态", self._check_stock_status, actions)
        actions.addStretch(1)
        action_column = QVBoxLayout()
        self.read_prices_button = self._button("读取材料参考价格", self._read_market_prices, action_column)
        action_column.addWidget(self._markup_widget)
        self.create_button = self._button("① 创建并初始上架", self._create, action_column)
        self.create_button.setObjectName("ecoCreateButton")
        self.create_button.setStyleSheet("""
            QPushButton#ecoCreateButton { background: #0891b2; color: white; font-weight: 700; }
            QPushButton#ecoCreateButton:enabled:hover { background: #2563eb; border-color: #60a5fa; }
            QPushButton#ecoCreateButton:enabled:pressed { background: #1d4ed8; }
            QPushButton#ecoCreateButton:disabled { background: #d1d5db; color: #9ca3af; border-color: #d1d5db; }
        """)
        self.private_listing_button = self._button("② 确认最终售价", self._publish_materials, action_column)
        self.private_listing_button.setObjectName("confirmDialogOkBtn")
        actions.addLayout(action_column)
        layout.addLayout(actions)
        layout.addStretch(1)
        self.account_combo.currentIndexChanged.connect(self._match_inventory)

    def _build_list_tab(self):
        layout = QVBoxLayout(self._list_tab)
        filters = QHBoxLayout()
        self.state_combo = QComboBox()
        for state, name in FURNACE_STATES.items():
            self.state_combo.addItem(name, state)
        filters.addWidget(self.state_combo)
        filters.addWidget(QLabel("页码"))
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 9999999)
        filters.addWidget(self.page_spin)
        self._button("查询 / 刷新", self._query, filters)
        filters.addStretch(1)
        layout.addLayout(filters)
        self.furnace_count_label = QLabel("查询合炉后显示平台状态和参考价值")
        self.furnace_count_label.setObjectName("alchemyStep1Hint")
        layout.addWidget(self.furnace_count_label)
        self.furnace_list = QListWidget()
        self.furnace_list.setSpacing(8)
        self.furnace_list.setStyleSheet("QListWidget::item { border: none; background: transparent; } QListWidget::item:selected { background: transparent; }")
        self.furnace_list.setFrameShape(QFrame.NoFrame)
        self.furnace_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.furnace_list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.furnace_list, 1)
        actions = QHBoxLayout()
        self._button("打开 ECO", lambda: QDesktopServices.openUrl(QUrl("https://www.ecosteam.cn/")), actions)
        actions.addStretch(1)
        self.deposit_hint_label = QLabel("请自行进入ECO APP缴纳押金")
        actions.addWidget(self.deposit_hint_label)
        layout.addLayout(actions)

    def _build_settings_tab(self):
        credentials = load_eco_credentials()
        layout = QVBoxLayout(self._settings_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        intro = QLabel("连接 ECO 开放平台")
        intro.setObjectName("recipeSavedTitle")
        layout.addWidget(intro)
        steps = QLabel("① 填写身份 ID    →    ② 生成或导入密钥    →    ③ 将公钥填入 ECO 后保存")
        steps.setObjectName("alchemyStep1Hint")
        steps.setWordWrap(True)
        layout.addWidget(steps)

        account_card = QFrame()
        account_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        account_card.setObjectName("recipeManageRow")
        account_card.setAttribute(Qt.WA_StyledBackground, True)
        account_box = QVBoxLayout(account_card)
        account_box.setContentsMargins(16, 14, 16, 16)
        account_box.setSpacing(10)
        account_title = QLabel("接入账号")
        account_title.setObjectName("recipeSavedTitle")
        account_box.addWidget(account_title)
        account_hint = QLabel("个人使用选择个人模式，身份 ID 可在 ECO APP 的开放平台设置中复制。")
        account_hint.setWordWrap(True)
        account_hint.setObjectName("alchemyStep1Hint")
        account_box.addWidget(account_hint)
        layout.addWidget(account_card)
        form = QFormLayout()
        self._settings_form = form
        self.credential_mode_combo = QComboBox()
        self.credential_mode_combo.addItem("个人ECO账号", "personal")
        self.credential_mode_combo.addItem("服务商模式（已获服务商授权）", "service")
        self.credential_mode_combo.setCurrentIndex(1 if credentials.service_provider_id or credentials.open_id else 0)
        form.addRow("接入方式", self.credential_mode_combo)
        self.partner_edit = QLineEdit(credentials.partner_id)
        self.partner_edit.setPlaceholderText("复制 ECO 开放平台设置中的「身份 ID」")
        self.key_edit = QLineEdit(credentials.private_key)
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("点击下方生成密钥自动填入；已有私钥可导入")
        self.service_edit = QLineEdit(credentials.service_provider_id)
        self.open_id_edit = QLineEdit(credentials.open_id)
        self.service_edit.setPlaceholderText("个人模式留空")
        self.open_id_edit.setPlaceholderText("个人模式留空")
        form.addRow("身份 ID（PartnerId）", self.partner_edit)
        form.addRow("服务商 ID（ServiceProviderId）", self.service_edit)
        form.addRow("授权用户 OpenID", self.open_id_edit)
        self.public_edit = QLineEdit()
        self.public_edit.setReadOnly(True)
        self.public_edit.setPlaceholderText("生成或导入密钥后，复制此公钥到 ECO")
        form.setVerticalSpacing(10)
        account_box.addLayout(form)
        key_card = QFrame()
        key_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        key_card.setObjectName("recipeManageRow")
        key_card.setAttribute(Qt.WA_StyledBackground, True)
        key_box = QVBoxLayout(key_card)
        key_box.setContentsMargins(16, 14, 16, 16)
        key_box.setSpacing(10)
        key_title = QLabel("RSA 签名密钥")
        key_title.setObjectName("recipeSavedTitle")
        key_box.addWidget(key_title)
        key_hint = QLabel("私钥由小助手加密保存在本机；公钥需填到 ECO「签名验证 → RSA 公钥」。")
        key_hint.setWordWrap(True)
        key_hint.setObjectName("alchemyStep1Hint")
        key_box.addWidget(key_hint)
        key_form = QFormLayout()
        key_form.setVerticalSpacing(10)
        key_form.addRow("本机私钥", self.key_edit)
        key_form.addRow("ECO 公钥", self.public_edit)
        key_box.addLayout(key_form)
        layout.addWidget(key_card)
        keys = QHBoxLayout()
        self._button("导入私钥文件", self._import_key, keys)
        self._button("生成 RSA 密钥", self._generate_key, keys)
        self._button("复制公钥", self._copy_public, keys)
        key_box.addLayout(keys)
        help_card = QFrame()
        help_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        help_card.setObjectName("recipeManageToolbar")
        help_card.setAttribute(Qt.WA_StyledBackground, True)
        help_box = QVBoxLayout(help_card)
        help_box.setContentsMargins(16, 12, 16, 12)
        help_title = QLabel("配置提示")
        help_title.setObjectName("recipeSavedTitle")
        help_box.addWidget(help_title)
        help_text = QLabel("• ECO 的「身份 ID」即 PartnerId，私钥在本机生成，无需向 ECO 索取。\n• 私密上架与改价需要开启出售权限，个人模式无需配置回调。\n• 保存后可到「我的合炉」查询，检查授权是否可用。")
        help_text.setObjectName("alchemyStep1Hint")
        help_text.setWordWrap(True)
        help_box.addWidget(help_text)
        layout.addWidget(help_card)
        layout.addStretch(1)
        actions = QHBoxLayout()
        self._button("官方文档", lambda: QDesktopServices.openUrl(QUrl("https://s.apifox.cn/8dd30eef-e628-4390-b503-846412c608ee")), actions)
        actions.addStretch(1)
        save_button = self._button("保存 API 设置", self._save_settings, actions)
        save_button.setObjectName("confirmDialogOkBtn")
        save_button.setMinimumWidth(150)
        layout.addLayout(actions)
        for edit in (self.partner_edit, self.key_edit, self.service_edit, self.open_id_edit):
            edit.textChanged.connect(self._clear_furnace_selection)
        self.credential_mode_combo.currentIndexChanged.connect(self._sync_credential_mode)
        self._sync_credential_mode()

    def _sync_credential_mode(self, _index=0):
        service = self.credential_mode_combo.currentData() == "service"
        self._settings_form.setRowVisible(self.partner_edit, not service)
        self._settings_form.setRowVisible(self.service_edit, service)
        self._settings_form.setRowVisible(self.open_id_edit, service)
        self._clear_furnace_selection()

    def _clear_furnace_selection(self, _text=""):
        self._created_furnace_id = ""
        self._workflow_furnace = {}
        self.workflow_furnace_combo.clear()
        self.workflow_furnace_combo.addItem("新建合炉 / 请选择要套用的已有合炉", None)
        self.reference_total_label.setText("材料总参考价值：创建后自动读取")
        self._update_totals()
        self._listed = []
        self.furnace_list.clear()
        self._sync_buttons()

    def _credentials(self):
        service = self.credential_mode_combo.currentData() == "service"
        credentials = EcoCredentials(
            "" if service else self.partner_edit.text().strip(),
            self.key_edit.text().strip(),
            self.service_edit.text().strip() if service else "",
            self.open_id_edit.text().strip() if service else "",
        )
        credentials.identity_params()
        public_key_for(credentials.private_key)
        return credentials

    def _save_settings(self):
        try:
            credentials = self._credentials()
            save_eco_credentials(credentials)
            self.public_edit.setText(public_key_for(credentials.private_key))
            self._set_status("API 设置已加密保存到本机。可先到「我的合炉」查询，测试 API 权限。")
        except (ValueError, OSError) as exc:
            self._set_status(str(exc), error=True)

    def _import_key(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择 RSA 私钥", "", "密钥文件 (*.pem *.key *.txt);;所有文件 (*)")
        if not filename:
            return
        try:
            text = Path(filename).read_text(encoding="utf-8-sig").strip()
            public_key = public_key_for(text)
            self.key_edit.setText(text)
            self.public_edit.setText(public_key)
            self._set_status("私钥已载入，请核对 PartnerId 后保存设置。")
        except (OSError, ValueError) as exc:
            self._set_status(str(exc), error=True)

    def _generate_key(self):
        if self.key_edit.text() and not ask_confirmation(self, "重新生成密钥", "重新生成后需要把新公钥更新到 ECO，原密钥将不再用于本程序。确定继续吗？"):
            return
        private, public = generate_key_pair()
        self.key_edit.setText(private)
        self.public_edit.setText(public)
        self._set_status("私钥已在本机生成并自动填入。点击「复制公钥」，到 ECO 开启签名验证并设置 RSA公钥，再把 ECO 的身份 ID 填到这里保存。")

    def _copy_public(self):
        try:
            value = public_key_for(self.key_edit.text())
            self.public_edit.setText(value)
            QApplication.clipboard().setText(value)
            self._set_status("公钥已复制，可粘贴到 ECO 开放平台。")
        except ValueError as exc:
            self._set_status(str(exc), error=True)

    def _match_inventory(self, _index=0):
        if not self._create_locked:
            self._created_furnace_id = ""
            self.reference_total_label.setText("材料总参考价值：创建后自动读取")
        if not self.recipe.get("substrates_display"):
            self._assets = []
            self.material_table.setRowCount(0)
            self._sync_buttons()
            return
        profile = str(self.account_combo.currentData() or "")
        inventory = load_profile_inventory_items(profile) if profile else []
        self._market_prices = {}
        self.read_prices_button.setText("读取材料参考价格")
        self.market_total_label.setText("ECO / BUFF同磨损区间参考价合计：未读取（不使用收料成本）")
        self._assets = match_recipe_inventory(self.recipe, inventory)
        if self._resume:
            for asset, substrate in zip(self._assets, self.recipe.get("substrates_display") or []):
                if not asset["AssetId"]:
                    asset["AssetId"] = str(substrate.get("steam_assetid") or "")
        self.material_table.setRowCount(len(self._assets))
        for index, asset in enumerate(self._assets):
            for col, text in enumerate((asset["name"], str(asset.get("float_value") or ""), asset["AssetId"] or "未匹配到库存")):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.material_table.setItem(index, col, item)
            price = QDoubleSpinBox()
            price.setDecimals(2)
            price.setRange(0, 1000000000)
            price.setStyleSheet("QDoubleSpinBox { min-height: 22px; padding: 0 6px; border-radius: 5px; }")
            price.ensurePolished()
            price.setFixedHeight(28)
            try:
                price.setValue(float(self._listing_prices.get(asset["AssetId"], self._market_prices.get(asset["AssetId"], 0)) or 0))
            except (ValueError, TypeError):
                price.setValue(0)
            self.material_table.setCellWidget(index, self.PRICE_COLUMN, price)
            price.valueChanged.connect(self._update_totals)
        self._fit_material_table()
        self._update_totals()
        missing = sum(not row["AssetId"] for row in self._assets)
        if not profile:
            self._set_status("暂无有效库存账号，请先在 Steam 库存登录账号并获取库存。", error=True)
        elif missing:
            self._set_status(f"有 {missing} 件材料未匹配到所选账号的精确库存，请先刷新 Steam 库存，再重新匹配。", error=True)
        elif self._assets:
            self._set_status("库存匹配完成。请先创建或套用合炉，再确认材料售价并私密上架；请自行进入ECO APP缴纳押金。")
        self._sync_buttons()

    def _fit_material_table(self):
        header = self.material_table.horizontalHeader()
        height = header.height() + self.material_table.frameWidth() * 2
        for row in range(self.material_table.rowCount()):
            self.material_table.setRowHeight(row, 32)
            height += self.material_table.rowHeight(row)
        self.material_table.setFixedHeight(height)

    def _update_totals(self, _value=None):
        sale_total = sum((Decimal(str(self.material_table.cellWidget(index, self.PRICE_COLUMN).value())) for index in range(self.material_table.rowCount()) if self.material_table.cellWidget(index, self.PRICE_COLUMN) is not None), Decimal("0"))
        self.sale_total_label.setText(f"炉主上架材料总售价：¥{sale_total:.2f}")

    def _missing_market_login_choice(self, platform, detail=""):
        prompt = QMessageBox(self)
        prompt.setWindowTitle(f"{platform} 尚未登录或授权")
        prompt.setText(f"读取 {platform} 报价前，请先完成登录或授权。\n{detail}")
        login = prompt.addButton("去登录 / 配置授权", QMessageBox.AcceptRole)
        skip = prompt.addButton("本次不登录，跳过此平台", QMessageBox.DestructiveRole)
        prompt.addButton("取消查询", QMessageBox.RejectRole)
        prompt.setDefaultButton(login)
        prompt.exec()
        return "login" if prompt.clickedButton() == login else "skip" if prompt.clickedButton() == skip else "cancel"

    def _login_quote_platform(self, provider):
        from ui.workers.market_login import MarketplaceLoginCaptureWorker

        class LoginDialog(QDialog):
            def reject(dialog):
                if not dialog.worker.isRunning():
                    super().reject()

        dialog = LoginDialog(self)
        dialog.setWindowTitle("完成平台登录")
        dialog.setMinimumSize(440, 160)
        layout = QVBoxLayout(dialog)
        hint = QLabel("请在打开的浏览器中完成登录；放弃登录请关闭浏览器窗口。", dialog)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        dialog.worker = MarketplaceLoginCaptureWorker(provider, dialog)
        dialog.worker.progress.connect(lambda _provider, text: hint.setText(text))
        dialog.worker.finished.connect(dialog.accept)
        dialog.worker.start()
        dialog.exec()

    def _read_market_prices(self):
        from core.market_candidates import provider_auth_available
        if self._worker is not None:
            return
        skipped = set()
        assets = [dict(asset) for asset in self._assets]
        if not assets or any(not asset["AssetId"] for asset in assets):
            self._set_status("请先匹配全部材料库存", error=True)
            return
        try:
            client = EcoFurnaceClient(self._credentials())
        except (ValueError, OSError) as exc:
            choice = self._missing_market_login_choice("ECO", str(exc))
            if choice == "login":
                self.tabs.setCurrentWidget(self._settings_tab)
                return
            if choice != "skip":
                return
            client = None
            skipped.add("eco")
        for provider, label in (("buff", "BUFF"),):
            while not provider_auth_available(provider):
                choice = self._missing_market_login_choice(label)
                if choice == "skip":
                    skipped.add(provider)
                    break
                if choice != "login":
                    return
                self._login_quote_platform(provider)
        if len(skipped) == 2:
            self._set_status("本次已跳过所有平台，未查询报价。")
            return
        steam_id = self._profiles.get(str(self.account_combo.currentData() or ""), "")
        self._start_request("market_prices", lambda: material_market_prices(client, steam_id, assets, skipped=skipped))

    def _increase_prices(self):
        if self._worker is not None or self._listing_uncertain:
            return
        prices = [self.material_table.cellWidget(i, self.PRICE_COLUMN) for i, asset in enumerate(self._assets) if asset["AssetId"] not in self._published_asset_ids]
        if not prices or any(price is None for price in prices):
            return
        assets = [asset for asset in self._assets if asset["AssetId"] not in self._published_asset_ids]
        if any(asset["AssetId"] not in self._market_prices for asset in assets):
            self._set_status("请先读取全部材料的 ECO / BUFF同磨损区间价格；缺失报价不能按成本替代。", error=True)
            return
        ratio = 1 + Decimal(str(self.markup_spin.value())) / 100
        updated = [(Decimal(str(self._market_prices[asset["AssetId"]])) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for asset in assets]
        if any(value <= 0 or value > Decimal(str(price.maximum())) for price, value in zip(prices, updated)):
            self._set_status("调整后有材料售价为零或超出上限，本次未修改。请调整溢价幅度。", error=True)
            return
        for price, value in zip(prices, updated):
            was_blocked = price.blockSignals(True)
            price.setValue(float(value))
            price.blockSignals(was_blocked)
        self._update_totals()
        self._set_status(f"已按参考价溢价 {self.markup_spin.value():g}% 设置 {len(prices)} 件材料售价，重复应用不会累加。")

    def _remember_furnace_title(self):
        title = self.title_edit.text().strip()
        if not title:
            return
        titles = load_app_settings().get("eco_recent_furnace_titles", [])
        titles = [title] + [value for value in titles if value != title]
        update_app_settings(updates={"eco_recent_furnace_titles": titles[:5]})
        edit_blocked = self.title_edit.blockSignals(True)
        self.title_combo.blockSignals(True)
        self.title_combo.clear()
        self.title_combo.addItems(titles[:5])
        self.title_edit.setText(title)
        self.title_combo.blockSignals(False)
        self.title_edit.blockSignals(edit_blocked)

    def _create_block_reason(self):
        if self._worker is not None:
            return "正在请求，请稍后..."
        if self._create_locked:
            return "合炉已提交，请刷新已有合炉，不要重复创建。"
        if not self.title_edit.text().strip():
            return "请先填写合炉标题。"
        if len(self._assets) not in (5, 10) or any(not row["AssetId"] for row in self._assets):
            return "材料未全部匹配库存，请先重新匹配库存。"
        missing = sum(row["AssetId"] not in self._market_prices or self._market_prices[row["AssetId"]] <= 0 for row in self._assets)
        if missing:
            return f"尚有 {missing} 件材料未取得有效参考价格，请先读取材料参考价格。"
        return ""

    def _sync_buttons(self):
        busy = self._worker is not None
        pricing = bool(self._workflow_furnace)
        self._step_label.setText("① 创建合炉  →  ② 确认最终售价" if not pricing else "① 合炉已创建  →  ② 确认最终售价")
        self._workflow_hint.setText(
            "请参考 ECO 材料总参考价值调整最终售价；确认后更新已有私密材料的价格，未上架的材料补上架。押金请在 ECO APP 缴纳。"
            if pricing else
            "建炉会提交材料并按初始价私密上架，创建后自动读取 ECO 材料总参考价值，再进入第二步调价。"
            if not self._create_locked else
            "合炉已提交，等待读取对应合炉及参考价值；请刷新查询，不要重复创建。"
        )
        self.material_table.horizontalHeaderItem(self.PRICE_COLUMN).setText("最终售价" if pricing else "初始售价")
        self.material_table.horizontalHeaderItem(self.PRICE_COLUMN).setToolTip("创建合炉后可修改最终售价" if not pricing else "确认后同步至 ECO 的实际私密售价")
        self._markup_widget.setVisible(pricing)
        self.create_button.setVisible(not self._create_locked)
        self.private_listing_button.setVisible(bool(self._workflow_furnace))
        remaining = any(row["AssetId"] not in self._published_asset_ids for row in self._assets)
        can_price = not busy and bool(self._workflow_furnace) and not self._listing_uncertain
        self.markup_spin.setEnabled(not busy and not self._listing_uncertain and remaining)
        self.markup_button.setEnabled(not busy and not self._listing_uncertain and remaining)
        previous_reason = getattr(self, "_create_disabled_reason", "")
        reason = self._create_block_reason()
        self._create_disabled_reason = reason
        self.create_button.setEnabled(not reason)
        self.create_button.setToolTip(reason)
        if reason and not pricing and hasattr(self, "status_label"):
            self._set_status(reason, error=True)
        elif previous_reason and not reason and hasattr(self, "status_label"):
            self._set_status("标题及材料参考价格已就绪，可以创建合炉。")
        self.private_listing_button.setEnabled(not busy and bool(self._workflow_furnace) and not self._listing_uncertain and remaining and all(row["AssetId"] for row in self._assets))
        self.stock_check_button.setEnabled(not busy and bool(self._assets))
        for index, asset in enumerate(self._assets):
            price = self.material_table.cellWidget(index, self.PRICE_COLUMN)
            if price is not None:
                price.setEnabled(can_price and asset["AssetId"] not in self._published_asset_ids)

    def _start_request(self, operation, callback):
        if self._worker is not None:
            return
        self._operation = operation
        self.tabs.setEnabled(False)
        self._set_status("正在请求，请稍后...")
        worker = _EcoRequestWorker(callback, self)
        worker.progress.connect(self._set_status)
        self._worker = worker
        worker.finished.connect(self._request_finished)
        self._sync_buttons()
        worker.start()

    def _request_finished(self):
        worker = self._worker
        refresh_furnaces = False
        self._worker = None
        self.tabs.setEnabled(True)
        if worker.error:
            if self._operation == "create" and worker.uncertain:
                self._create_locked = True
            if self._operation == "create" and self._create_payload is not None:
                self.publication_changed.emit("uncertain" if worker.uncertain else "unlisted", self._create_payload, "")
            if self._operation == "publish_materials":
                self._listing_uncertain = worker.uncertain
                self._save_listing_progress()
            self._set_status(worker.error, error=True)
        elif self._operation == "market_prices":
            self._market_prices = dict(worker.result["prices"])
            for index, asset in enumerate(self._assets):
                if asset["AssetId"] not in self._published_asset_ids:
                    self.material_table.cellWidget(index, self.PRICE_COLUMN).setValue(self._market_prices.get(asset["AssetId"], 0))
            self._update_totals()
            for index, asset in enumerate(self._assets):
                quotes = worker.result["platforms"][asset["AssetId"]]
                detail = "；".join(f"{platform}：¥{quotes[platform]:.2f}" if platform in quotes else f"{platform}：未取得报价" for platform in ("ECO", "BUFF"))
                winner = min(quotes, key=quotes.get) if quotes else "无"
                self.material_table.cellWidget(index, self.PRICE_COLUMN).setToolTip(f"{detail}；采用来源：{winner}")
            total = sum(Decimal(str(value)) for value in self._market_prices.values())
            self.market_total_label.setText(f"ECO / BUFF同磨损区间参考价合计：¥{total:.2f} · 已报价 {len(self._market_prices)}/{len(self._assets)} 件")
            self.read_prices_button.setText(f"材料参考价合计：¥{total:.2f}" if len(self._market_prices) == len(self._assets) else "读取材料参考价格（报价不完整，点击重试）")
            self._set_status("；".join(worker.result["errors"]) or "材料参考价格读取完成。")
            if self._workflow_furnace:
                self._increase_prices()
        elif self._operation == "create":
            self._create_locked = True
            response = worker.result["response"]
            furnace_id = str(response.get("FurnaceID") or "") if isinstance(response, dict) else ""
            self._pre_create_furnace_ids = set(worker.result["before_ids"])
            self._awaiting_new_furnace = not bool(furnace_id)
            self._created_furnace_id = furnace_id
            self.reference_total_label.setText("材料总参考价值：请到「我的合炉」查询")
            if self._create_payload is not None:
                self.publication_changed.emit("submitted", self._create_payload, furnace_id)
            self._sync_publish_view()
            self._show_furnaces({"PageResult": worker.result["furnaces"], "TotalRecord": len(worker.result["furnaces"])})
            self._set_status("合炉已创建并自动套用，请核对参考价值和材料售价。" if self._workflow_furnace else "合炉已创建，等待平台列表更新；请刷新读取参考价值，无需再次创建。")
            self.state_combo.setCurrentIndex(self.state_combo.findData(100))
            self.page_spin.setValue(1)
            if self.isVisible() and not self._workflow_furnace:
                QTimer.singleShot(1000, self._refresh_workflow_furnaces)
        elif self._operation == "publish_materials":
            expected = {row["AssetId"] for row in self._listing_assets}
            returned = {str(row.get("AssetId") or ""): row for row in worker.result if isinstance(row, dict)}
            succeeded = {asset_id for asset_id in expected if returned.get(asset_id, {}).get("IsSuccess") is True}
            self._published_asset_ids.update(succeeded)
            self._listing_uncertain = any(asset_id not in returned or returned[asset_id].get("IsSuccess") not in (True, False) for asset_id in expected)
            self._save_listing_progress()
            errors = [str(returned[asset_id].get("ErrorMsg") or "上架失败") for asset_id in expected if returned.get(asset_id, {}).get("IsSuccess") is False]
            self._set_status(f"本次上架或改价已确认 {len(succeeded)}/{len(expected)} 件。" + ("部分实际售价待确认，请先核对材料上架状态。" if self._listing_uncertain else "；".join(errors) if errors else "请核对材料是否已入炉。"), error=self._listing_uncertain or bool(errors))
            if not self._listing_uncertain and all(row["AssetId"] in self._published_asset_ids for row in self._assets):
                self.tabs.setCurrentWidget(self._list_tab)
                self.state_combo.setCurrentIndex(self.state_combo.findData(100))
                self.page_spin.setValue(1)
                refresh_furnaces = True
        elif self._operation == "stock_status":
            stock, listed = worker.result["stock"], worker.result["listed"]
            wanted = {row["AssetId"] for row in self._assets}
            prices = {row["AssetId"]: self.material_table.cellWidget(index, self.PRICE_COLUMN).value() for index, row in enumerate(self._assets)}
            self._published_asset_ids = {asset_id for asset_id in wanted if asset_id in listed and str(listed[asset_id].get("Price")) and Decimal(str(listed[asset_id].get("Price"))) == Decimal(str(prices[asset_id]))}
            self._listing_uncertain = not all(asset_id in listed or stock.get(asset_id, {}).get("CanPublish") is True for asset_id in wanted)
            self._save_listing_progress()
            self._set_status(f"已确认 {len(self._published_asset_ids)}/{len(wanted)} 件材料的私密售价一致。" + ("其余材料状态仍需在 ECO 核对。" if self._listing_uncertain else "其余材料可继续确认售价并上架或改价。"), error=self._listing_uncertain)
        elif self._operation == "list":
            self._show_furnaces(worker.result)
        if self._operation == "list" and worker.error:
            self._workflow_furnace = {}
            self._listed = []
            self.furnace_list.clear()
        self._sync_buttons()
        worker.deleteLater()
        if refresh_furnaces:
            self._query()

    def _create(self):
        logging.getLogger(__name__).warning("ECO create clicked: locked=%s busy=%s", self._create_locked, self._worker is not None)
        if self._create_locked or self._worker is not None:
            self._set_status(self._create_block_reason(), error=True)
            return
        if not self._assets or any(row["AssetId"] not in self._market_prices for row in self._assets):
            self._set_status("请先成功读取全部材料参考价格。", error=True)
            return
        try:
            client = EcoFurnaceClient(self._credentials())
            assets = [dict(row, Price=self.material_table.cellWidget(index, self.PRICE_COLUMN).value()) for index, row in enumerate(self._assets)]
            steam_id = self._profiles.get(str(self.account_combo.currentData() or ""), "")
            payload = build_create_payload(steam_id, self.title_edit.text(), assets, fee_rate=self.fee_spin.value(), publish_state=self.publish_combo.currentData(), free_open_fee=self.free_fee_check.isChecked(), self_open=self.self_open_check.isChecked())
        except (ValueError, OSError) as exc:
            self._set_status(str(exc), error=True)
            return
        if not ask_confirmation(self, "创建 ECO 合炉", f"账号：{self.account_combo.currentText()}\n标题：{payload['Title']}\n{self.publish_combo.currentText()} · {len(assets)} 件材料\n开炉费：{payload['FurnaceOpenRate']:g}% · 人齐自开：{'开启' if payload['SelfOpen'] else '关闭'}\n创建接口会提交材料及初始售价；下一步确认最终售价并更新已有私密上架。请自行进入ECO APP缴纳押金。"):
            self._set_status("已取消创建，未发送请求。")
            return
        try:
            self._remember_furnace_title()
        except (OSError, ValueError) as exc:
            self._set_status(f"标题记录保存失败，尚未创建合炉：{exc}", error=True)
            return
        self._create_payload = copy.deepcopy(payload)
        logging.getLogger(__name__).warning("ECO create confirmed; saving pending state")
        self.publication_changed.emit("pending", self._create_payload, "")
        if self._create_locked:
            self._sync_buttons()
            return
        def create_and_lookup():
            self._worker.progress.emit("正在查询已有合炉，准备提交创建请求...")
            logging.getLogger(__name__).warning("ECO create: querying existing furnaces")
            before = client.list_all_furnaces()
            self._worker.progress.emit("正在向 ECO 提交创建请求，请勿重复点击...")
            logging.getLogger(__name__).warning("ECO create: sending request")
            response = client.create(payload)
            self._worker.progress.emit("ECO 已确认创建成功，正在读取合炉以进入第二步...")
            logging.getLogger(__name__).warning("ECO create: succeeded; querying resulting furnace")
            try:
                furnaces = client.list_all_furnaces()
            except EcoApiError:
                furnaces = []  # Creation succeeded; a failed read must not unlock another create.
            return {"response": response, "before_ids": [str(row["FurnaceID"]) for row in before], "furnaces": furnaces}
        self._start_request("create", create_and_lookup)

    def _query(self):
        try:
            client = EcoFurnaceClient(self._credentials())
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        state, page = self.state_combo.currentData(), self.page_spin.value()
        self._start_request("list", lambda: client.list_furnaces_with_prices(state, page))

    def _show_furnaces(self, data):
        self._listed = [row for row in data["PageResult"] if isinstance(row, dict)]
        sync_error = ""
        try:
            sync_furnace_publications(self._listed)
        except (OSError, ValueError) as exc:
            sync_error = f"平台状态已读取，本地标记同步失败：{exc}"
        self._populate_workflow_furnaces()
        self.furnace_list.clear()
        self.furnace_count_label.setText(f"本页 {len(self._listed)} 炉 · 平台共 {data.get('TotalRecord', len(self._listed))} 炉" if self._listed else "当前状态暂无合炉，可切换状态或刷新查看。")
        for index, row in enumerate(self._listed):
            reference = _furnace_reference_text(row)
            state = str(row.get("ShowState") or FURNACE_STATES.get(row.get("HL_FurnaceState", row.get("FurnaceState")), "状态未返回"))
            current = str(row.get("FurnaceID") or "") == self._created_furnace_id
            card = QFrame()
            card.setObjectName("recipeManageRow")
            card.setAttribute(Qt.WA_StyledBackground, True)
            box = QVBoxLayout(card)
            box.setContentsMargins(16, 12, 16, 12)
            top = QHBoxLayout()
            top.setSpacing(16)
            title = QLabel(str(row.get("Title") or "未命名合炉"))
            title.setTextFormat(Qt.PlainText)
            title.setObjectName("recipeSavedTitle")
            top.addWidget(title)
            if current:
                badge = QLabel("当前配方")
                badge.setStyleSheet("QLabel { color: #078db5; font-weight: 600; }")
                top.addWidget(badge)
            top.addStretch(1)
            status = QLabel(state)
            status.setStyleSheet("QLabel { color: #078db5; font-weight: 700; padding: 4px 10px; background: rgba(7,141,181,25); border-radius: 6px; }")
            top.addWidget(status)
            box.addLayout(top)
            middle = QHBoxLayout()
            caption = QLabel("材料总参考价值")
            caption.setObjectName("alchemyStep1Hint")
            middle.addWidget(caption)
            value = QLabel(reference)
            value.setObjectName("ecoFurnaceReferenceValue")
            value.setStyleSheet("QLabel { color: #078db5; font-size: 20px; font-weight: 700; }")
            middle.addWidget(value)
            middle.addSpacing(24)
            listed_caption = QLabel("材料上架总价")
            listed_caption.setObjectName("alchemyStep1Hint")
            middle.addWidget(listed_caption)
            total = row.get("_listing_total")
            listed_value = QLabel(f"¥{total:.2f}" if total is not None else "未确认")
            listed_value.setObjectName("ecoFurnaceListingTotal")
            listed_value.setStyleSheet("QLabel { color: #16a34a; font-size: 20px; font-weight: 700; }")
            listed_value.setToolTip(row.get("_listing_total_hint") or "查询 / 刷新后核对实际私密在售价格")
            middle.addWidget(listed_value)
            middle.addStretch(1)
            confirmed = furnace_is_published(row)
            deposit = QLabel("平台已确认上合炉" if confirmed else "待缴押金 · 请前往 ECO APP" if row.get("CanPayment") is True else "押金状态以 ECO APP 为准")
            deposit.setStyleSheet(f"QLabel {{ color: {'#16a34a' if confirmed else '#d97706'}; font-weight: 600; }}")
            middle.addWidget(deposit)
            box.addLayout(middle)
            bottom = QHBoxLayout()
            detail = QLabel(f"合炉已添加材料：{row.get('AddMatCount', '未返回')} 件")
            detail.setObjectName("alchemyStep1Hint")
            detail.setToolTip("ECO 返回的 AddMatCount，非私密上架数量。")
            bottom.addWidget(detail)
            bottom.addStretch(1)
            furnace_id = str(row.get("FurnaceID") or "")
            copy = QPushButton("复制炉号")
            copy.setEnabled(bool(furnace_id))
            copy.setToolTip(furnace_id)
            copy.clicked.connect(lambda _checked=False, value=furnace_id: QApplication.clipboard().setText(value))
            bottom.addWidget(copy)
            box.addLayout(bottom)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, {"furnace_id": furnace_id, "reference": reference})
            item.setSizeHint(QSize(0, 154))
            self.furnace_list.addItem(item)
            self.furnace_list.setItemWidget(item, card)
            if current:
                self.furnace_list.setCurrentRow(index)
        self._set_status(f"已查询 {len(self._listed)} 炉，参考价值和材料状态以 ECO 返回为准。请自行进入ECO APP缴纳押金")
        if sync_error:
            self._set_status(sync_error, error=True)
        self._sync_buttons()

    def reject(self):
        if self._worker is not None:
            self._set_status("请求进行中，请等待 ECO 返回后再关闭。")
            return
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            event.ignore()
            return
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
        if (self._resume or self.tabs.currentWidget() is self._list_tab) and not self._workflow_auto_started:
            self._workflow_auto_started = True
            QTimer.singleShot(0, self._refresh_workflow_furnaces)
