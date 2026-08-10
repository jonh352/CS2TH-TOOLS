"""Marketplace shortcuts, login hints and CS2TH recipe-material deep links."""

from __future__ import annotations

from datetime import datetime
import time

from PySide6.QtCore import QTimer, QUrl, QStringListModel, Qt, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import ASSETS_DIR, AUTH_API_BASE_URL, MATERIAL_COLLECTION_HISTORY_FILE
from core.alchemy_quality import (
    get_name_map,
    get_pid_map,
    normalize_name,
    strip_appearance_suffix_from_goods_name,
)
from core.auth_client import AuthClient
from core.collected_json import save_collected_json
from core.app_settings_store import load_app_settings, update_app_settings
from core.data_utils import SkinInstance, get_interval_value
from core.special_wear_materials import neighboring_purchase_interval
from core.platform_links import (
    MARKETPLACES,
    links_for_recipe_material,
    links_for_template,
)
from core.platform_login_state import (
    clear_confirmed_marketplace_logins,
    confirmed_marketplace_logins,
    set_marketplace_login_confirmed,
    steam_session_available,
)
from core.recipe_bridge import material_wear_range, saved_recipe_to_bridge_payload
from core.saved_recipes import list_saved_recipes
from core.json_store import read_json_dict, write_json
from core.market_candidates import (
    APP_LOGIN_PROVIDERS,
    EXACT_WEAR_PROVIDERS,
    clear_provider_auth,
    clear_c5_session_auth,
    provider_display_name,
)
from core.special_wear_names import get_skin_full_names_without_appearance
from ui.components import panel
from ui.dialogs.wide_text_input_dialog import get_wide_text_input
from ui.widgets.eliding_label import ElidingLabel
from ui.feedback import ask_confirmation
from ui.widgets.toast import show_toast
from ui.widgets.wear_interval_bar import WearIntervalBar, WearRangeSelector
from ui.workers.recipe_bridge import RecipeAlternativesThread, RecipeLoadThread
from ui.workers.market_login import (
    MarketplaceLoginCaptureWorker,
    MarketplaceLoginValidationWorker,
)
from ui.workers.material_collection import (
    MaterialCollectionWorker,
    dedupe_candidates_keep_cheapest,
)
from ui.workers.special_collection import SpecialCollectionWorker


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)  # type: ignore[arg-type]


_COLLECTION_PLATFORM_LABELS = (
    ("buff", "BUFF"),
    ("yyyp", "悠悠"),
    ("c5", "C5"),
    ("eco", "ECO"),
)


def format_collection_platform_counts(items: object) -> str:
    """Return a stable per-platform summary for collected listings."""
    counts = {key: 0 for key, _label in _COLLECTION_PLATFORM_LABELS}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("platform") or "").strip().lower()
            if key in counts:
                counts[key] += 1
    detail = "｜".join(
        f"{label} {counts[key]} 条" for key, label in _COLLECTION_PLATFORM_LABELS
    )
    return f"{detail}，共 {sum(counts.values())} 条"


class PlatformPage(QWidget):
    import_to_simulation_requested = Signal(object)
    import_to_alchemy_requested = Signal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._recipe_thread: RecipeLoadThread | RecipeAlternativesThread | None = None
        self._login_confirmed = confirmed_marketplace_logins()
        self._verified_logins: dict[str, dict] = {}
        self._login_validation_worker: MarketplaceLoginValidationWorker | None = None
        self._login_capture_worker: MarketplaceLoginCaptureWorker | None = None
        self._login_buttons: dict[str, QLabel] = {}
        self._market_open_buttons: dict[str, QPushButton] = {}
        self._collection_intervals: dict[str, QSpinBox] = {}
        self._source_checks: dict[str, QCheckBox] = {}
        self._collection_links: dict[str, list[tuple[str, str]]] = {
            market.key: [] for market in MARKETPLACES
        }
        # (platform_key, material_name, url)
        self._collection_queue: list[tuple[str, str, str]] = []
        self._collection_platform = ""
        self._collection_running = False
        self._collection_stopping = False
        self._recipe_material_states: list[dict] = []
        self._collection_timer = QTimer(self)
        self._collection_timer.setSingleShot(True)
        self._collection_timer.timeout.connect(self._process_next_collection_link)
        self._special_worker: SpecialCollectionWorker | None = None
        self._special_stopping = False
        self._material_worker: MaterialCollectionWorker | None = None
        self._pending_alchemy_import: list[dict] = []
        self._collected_items: list[dict] = []
        self._last_scrape_message = ""
        self._eco_retry_materials: list[dict] = []
        self._c5_retry_materials: list[dict] = []
        self._eco_retry_base_items: list[dict] = []
        self._allow_platform_retry_prompt = False
        self._pending_retry_provider: str = ""
        self._collection_scrape_pending = False
        self._collection_started_at: float | None = None
        self._special_payload: dict = {}
        self._special_slot_count = 10
        saved_settings = load_app_settings()
        raw_intervals = saved_settings.get("material_collection_intervals")
        self._saved_intervals = raw_intervals if isinstance(raw_intervals, dict) else {}
        self._saved_silent = bool(saved_settings.get("material_collection_silent", False))
        self._saved_include_alternatives = bool(
            saved_settings.get("material_collection_include_alternatives", False)
        )
        self._last_recipe_payload: dict | None = None
        raw_sources = saved_settings.get("special_candidate_sources")
        self._saved_sources = raw_sources if isinstance(raw_sources, dict) else {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setObjectName("platformScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(26, 12, 26, 24)
        root.setSpacing(16)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        root.addWidget(self._build_login_panel())

        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_skin_page())
        self.mode_stack.addWidget(self._build_recipe_page())
        self.mode_stack.addWidget(self._build_special_materials_page())
        root.addWidget(self.mode_stack)
        root.addStretch(1)
        self._set_mode(1)  # 默认进入「配方链接」
        self._refresh_login_states()

    def _build_login_panel(self) -> QFrame:
        frame, layout = panel(self)
        heading = QHBoxLayout()
        title = QLabel("平台账号")
        title.setObjectName("sectionTitle")
        note = QLabel("选择功能模式，并确认需要使用的平台登录状态")
        note.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(note)
        heading.addStretch(1)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.mode_buttons = []
        for index, text in enumerate(("单件饰品", "配方链接", "特殊磨损材料")):
            button = QPushButton(text)
            button.setObjectName("platformModeButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, value=index: self._set_mode(value))
            group.addButton(button)
            heading.addWidget(button)
            self.mode_buttons.append(button)
        self.login_validate_button = QPushButton("校验登录")
        self.login_validate_button.setToolTip(
            "校验本 APP 采集用登录态；各平台均走与「开始采集」相同的在售接口"
        )
        self.login_validate_button.clicked.connect(self._validate_marketplace_logins)
        self.clear_login_button = QPushButton("清除登录")
        self.clear_login_button.setObjectName("dangerOutlineButton")
        self.clear_login_button.setToolTip(
            "选择清除 BUFF / 悠悠 / C5GAME / ECO 的 APP 登录凭证"
        )
        self.clear_login_button.clicked.connect(self._show_clear_login_menu)
        heading.addWidget(self.login_validate_button)
        heading.addWidget(self.clear_login_button)
        layout.addLayout(heading)

        rows = QVBoxLayout()
        rows.setSpacing(0)
        for market in MARKETPLACES:
            row = QFrame()
            row.setObjectName("marketAccountRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 10, 8)
            row_layout.setSpacing(10)
            logo = QLabel()
            logo.setFixedSize(26, 26)
            logo_path = ASSETS_DIR / market.logo_name
            if logo_path.is_file():
                logo.setPixmap(QIcon(str(logo_path)).pixmap(24, 24))
            name = QLabel(market.name)
            name.setObjectName("marketAccountName")
            name.setFixedWidth(94)
            state = QLabel()
            state.setObjectName("marketLoginState")
            state.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            state.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            action = QPushButton("登录 / 打开")
            action.setObjectName("marketOpenButton")
            action.setFixedWidth(104)
            action.clicked.connect(
                lambda _=False, key=market.key: self._open_platform_account(key)
            )
            interval_label = QLabel("间隔")
            interval_label.setObjectName("muted")
            interval = QSpinBox()
            min_interval = 5 if market.key == "c5" else 3
            default_interval = min_interval
            interval.setRange(min_interval, 120)
            interval.setSuffix(" 秒")
            saved_interval = int(
                self._saved_intervals.get(market.key, default_interval)
            )
            interval.setValue(max(min_interval, saved_interval))
            interval.setFixedWidth(76)
            if market.key == "c5":
                interval.setToolTip(
                    "同一平台：翻页间隔，以及换下一种材料前的额外等待；"
                    "默认 5 秒（最短 5 秒）"
                )
            else:
                interval.setToolTip(
                    "同一平台：翻页间隔，以及换下一种材料前的额外等待；"
                    "默认 3 秒（最短 3 秒）"
                )
            interval.valueChanged.connect(self._save_collection_settings)
            source = QCheckBox("候选源")
            source.setChecked(
                bool(
                    self._saved_sources.get(
                        market.key,
                        market.key in EXACT_WEAR_PROVIDERS,
                    )
                )
            )
            source.toggled.connect(self._save_collection_settings)
            row_layout.addWidget(logo)
            row_layout.addWidget(name)
            row_layout.addWidget(state, 1)
            row_layout.addWidget(source)
            row_layout.addWidget(interval_label)
            row_layout.addWidget(interval)
            row_layout.addWidget(action)
            self._login_buttons[market.key] = state
            self._market_open_buttons[market.key] = action
            self._collection_intervals[market.key] = interval
            self._source_checks[market.key] = source
            rows.addWidget(row)
        layout.addLayout(rows)

        self.collection_controls_widget = QWidget()
        collection_controls = QHBoxLayout(self.collection_controls_widget)
        collection_controls.setContentsMargins(0, 0, 0, 0)
        self.silent_collection = QCheckBox("静默采集")
        self.silent_collection.setChecked(self._saved_silent)
        self.silent_collection.setToolTip(
            "勾选：不自动打开商品页；C5 用最小化系统窗口拉取挂单（采完即关），"
            "失败或风控则本轮停止 C5、不重试；"
            "ECO 遇访问限制时：有明确验证信号才弹窗，否则静默重试最多 3 轮，"
            "仍失败则本轮暂停该平台，其他平台采完后可询问是否重试。"
            "不勾选：可额外打开材料商品页"
        )
        self.silent_collection.toggled.connect(self._save_collection_settings)
        self.include_alternatives = QCheckBox("备选材料")
        self.include_alternatives.setChecked(self._saved_include_alternatives)
        self.include_alternatives.setToolTip(
            "开启后，读取配方时一并显示同收藏品同品级的备选材料（字体较小）"
        )
        self.include_alternatives.toggled.connect(self._on_include_alternatives_toggled)
        self.collection_status = ElidingLabel(
            "勾选候选源后点开始采集：完成后可导入计算或保存为 JSON"
        )
        self.collection_status.setObjectName("muted")
        self.collection_toggle_button = QPushButton("开始采集")
        self.collection_toggle_button.setObjectName("marketCollectButton")
        self.collection_toggle_button.setFixedWidth(104)
        self.collection_toggle_button.setToolTip(
            "BUFF / 悠悠 / C5 / ECO：抓取精确磨损挂单；"
            "Steam 打开材料链接。采集中可再次点击停止"
        )
        self.collection_toggle_button.clicked.connect(self._toggle_collection)
        self.collection_import_button = QPushButton("导入计算")
        self.collection_import_button.setObjectName("collectionImportButton")
        self.collection_import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collection_import_button.hide()
        self.collection_import_button.clicked.connect(
            self._import_collected_items_to_alchemy
        )
        self.collection_save_json_button = QPushButton("保存为 JSON")
        self.collection_save_json_button.hide()
        self.collection_save_json_button.clicked.connect(
            self._save_collected_items_as_json
        )
        collection_controls.addWidget(self.silent_collection, 0)
        collection_controls.addWidget(self.include_alternatives, 0)
        collection_controls.addWidget(self.collection_status, 1)
        collection_controls.addWidget(self.collection_save_json_button, 0)
        collection_controls.addWidget(self.collection_import_button, 0)
        collection_controls.addWidget(self.collection_toggle_button, 0)
        collection_controls.setStretch(2, 1)
        self._login_collection_host = QVBoxLayout()
        self._login_collection_host.setContentsMargins(0, 0, 0, 0)
        self._login_collection_host.addWidget(self.collection_controls_widget)
        layout.addLayout(self._login_collection_host)
        return frame

    def _build_skin_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        search_frame, search = panel(page)
        row = QHBoxLayout()
        label = QLabel("饰品名称")
        label.setObjectName("platformFieldLabel")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("输入并选择饰品名称")
        self.name_edit.setClearButtonEnabled(True)
        row.addWidget(label)
        row.addWidget(self.name_edit, 1)
        search.addLayout(row)
        range_row = QHBoxLayout()
        range_title = QLabel("采集磨损")
        range_title.setObjectName("platformFieldLabel")
        self.single_range_label = QLabel("选择饰品后可拖动左右手柄")
        self.single_range_label.setObjectName("recipeBridgeWear")
        range_row.addWidget(range_title)
        range_row.addWidget(self.single_range_label)
        range_row.addStretch(1)
        search.addLayout(range_row)
        self.single_range_selector = WearRangeSelector()
        self.single_range_selector.setEnabled(False)
        self.single_range_selector.rangeChanged.connect(
            self._on_single_range_changed
        )
        search.addWidget(self.single_range_selector)
        self.hint = QLabel("未选择饰品时，按钮打开平台市场首页。")
        self.hint.setObjectName("muted")
        search.addWidget(self.hint)
        layout.addWidget(search_frame)
        self._skin_collection_host = QVBoxLayout()
        self._skin_collection_host.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._skin_collection_host)

        names = get_skin_full_names_without_appearance()
        completer = QCompleter(QStringListModel(names, self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.name_edit.setCompleter(completer)
        self.name_edit.textChanged.connect(self._on_single_skin_changed)
        return page

    def _build_recipe_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        input_frame, input_layout = panel(page)
        row = QHBoxLayout()
        label = QLabel("配方页 URL")
        label.setObjectName("platformFieldLabel")
        self.recipe_edit = QLineEdit()
        self.recipe_edit.setClearButtonEnabled(True)
        self.recipe_edit.setPlaceholderText(
            "粘贴配方链接，例如 https://cs2th.cn/recipe/…?market=spot"
        )
        self.recipe_load_button = QPushButton("读取配方")
        self.recipe_load_button.setObjectName("recipeLoadButton")
        self.recipe_load_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recipe_load_button.clicked.connect(self._load_recipe)
        self.saved_recipe_button = QPushButton("从配方管理导入")
        self.saved_recipe_button.clicked.connect(self._choose_saved_recipe)
        self.recipe_edit.returnPressed.connect(self._load_recipe)
        row.addWidget(label)
        row.addWidget(self.recipe_edit, 1)
        row.addWidget(self.saved_recipe_button)
        row.addWidget(self.recipe_load_button)
        input_layout.addLayout(row)
        self.recipe_status = QLabel(
            "粘贴 CS2TH 配方链接并读取，或从配方管理 / 采集预设导入材料。"
            "读取后可调整磨损区间，再勾选候选源开始采集。"
        )
        self.recipe_status.setObjectName("muted")
        self.recipe_status.setWordWrap(True)
        input_layout.addWidget(self.recipe_status)
        layout.addWidget(input_frame)

        self._recipe_collection_host = QVBoxLayout()
        self._recipe_collection_host.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._recipe_collection_host)

        self.recipe_summary = QFrame()
        self.recipe_summary.setObjectName("recipeBridgeSummary")
        summary_layout = QHBoxLayout(self.recipe_summary)
        summary_layout.setContentsMargins(16, 12, 16, 12)
        self.recipe_summary_title = QLabel()
        self.recipe_summary_title.setObjectName("recipeSavedTitle")
        self.recipe_summary_meta = QLabel()
        self.recipe_summary_meta.setObjectName("muted")
        self.open_recipe_button = QPushButton("查看原配方")
        self.open_recipe_button.clicked.connect(self._open_current_recipe)
        summary_layout.addWidget(self.recipe_summary_title)
        summary_layout.addStretch(1)
        summary_layout.addWidget(self.recipe_summary_meta)
        summary_layout.addWidget(self.open_recipe_button)
        self.recipe_summary.hide()
        layout.addWidget(self.recipe_summary)

        materials_frame, materials_outer = panel(page)
        materials_frame.setObjectName("platformMaterialsPanel")
        materials_heading = QHBoxLayout()
        material_title = QLabel("配方材料")
        material_title.setObjectName("sectionTitle")
        self.material_count = QLabel("尚未读取")
        self.material_count.setObjectName("muted")
        materials_heading.addWidget(material_title)
        materials_heading.addStretch(1)
        materials_heading.addWidget(self.material_count)
        materials_outer.addLayout(materials_heading)

        self.materials_empty = self._build_recipe_materials_empty(page)
        materials_outer.addWidget(self.materials_empty)

        self.materials_list_host = QWidget()
        self.materials_layout = QVBoxLayout(self.materials_list_host)
        self.materials_layout.setContentsMargins(0, 0, 0, 0)
        self.materials_layout.setSpacing(10)
        self.materials_list_host.hide()
        materials_outer.addWidget(self.materials_list_host)
        layout.addWidget(materials_frame, 1)
        return page

    def _build_recipe_materials_empty(self, parent: QWidget) -> QFrame:
        """Guided empty state before a recipe / preset is loaded."""
        frame = QFrame(parent)
        frame.setObjectName("platformMaterialsEmpty")
        frame.setMinimumHeight(200)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(28, 28, 28, 28)
        lay.setSpacing(12)
        lay.addStretch(1)

        title = QLabel("还没有可采集的材料")
        title.setObjectName("platformMaterialsEmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        hint = QLabel(
            "先导入配方或采集预设，再勾选上方候选源并开始采集。\n"
            "支持三种方式："
        )
        hint.setObjectName("platformMaterialsEmptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        lay.addWidget(hint)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)

        focus_url = QPushButton("① 粘贴链接后读取")
        focus_url.setObjectName("alchemySelectFileBtn")
        focus_url.setCursor(Qt.CursorShape.PointingHandCursor)
        focus_url.setToolTip("聚焦配方链接输入框，粘贴后点「读取配方」")
        focus_url.clicked.connect(self._focus_recipe_url_for_load)

        from_manage = QPushButton("② 从配方管理导入")
        from_manage.setObjectName("primaryButton")
        from_manage.setCursor(Qt.CursorShape.PointingHandCursor)
        from_manage.clicked.connect(self._choose_saved_recipe)

        from_preset = QPushButton("③ 从采集预设导入")
        from_preset.setObjectName("alchemySelectFileBtn")
        from_preset.setCursor(Qt.CursorShape.PointingHandCursor)
        from_preset.setToolTip("前往「采集预设」选择方案并导入采集")
        from_preset.clicked.connect(self._open_collection_presets_page)

        actions.addWidget(focus_url)
        actions.addWidget(from_manage)
        actions.addWidget(from_preset)
        actions.addStretch(1)
        lay.addLayout(actions)

        tip = QLabel("导入后可在下方调整每种材料的采集磨损区间")
        tip.setObjectName("platformMaterialsEmptyHint")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch(1)
        return frame

    def _sync_recipe_materials_empty_state(self) -> None:
        has_materials = bool(self._recipe_material_states)
        if hasattr(self, "materials_empty"):
            self.materials_empty.setVisible(not has_materials)
        if hasattr(self, "materials_list_host"):
            self.materials_list_host.setVisible(has_materials)
        if not has_materials and hasattr(self, "material_count"):
            self.material_count.setText("尚未读取")

    def _set_recipe_load_emphasized(self, on: bool) -> None:
        self.recipe_load_button.setProperty("emphasized", bool(on))
        self.recipe_load_button.style().unpolish(self.recipe_load_button)
        self.recipe_load_button.style().polish(self.recipe_load_button)

    def _focus_recipe_url_for_load(self) -> None:
        self.recipe_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.recipe_edit.selectAll()
        self._set_recipe_load_emphasized(True)
        show_toast(self, "请粘贴配方链接，然后点「读取配方」", style="info")

    def _open_collection_presets_page(self) -> None:
        window = self.window()
        activate = getattr(window, "_activate", None)
        if callable(activate):
            activate("collection_presets")
            return
        show_toast(self, "无法打开采集预设页", style="warning")

    def _build_special_materials_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        summary, summary_layout = panel(page)
        self.special_source_title = QLabel("请先在“特殊磨损”中查询材料")
        self.special_source_title.setObjectName("sectionTitle")
        self.special_source_meta = QLabel(
            "查询完成后点击“前往材料采集”，材料和可采购区间会自动显示在这里。"
        )
        self.special_source_meta.setObjectName("muted")
        self.special_source_meta.setWordWrap(True)
        summary_top = QHBoxLayout()
        summary_text = QVBoxLayout()
        summary_text.addWidget(self.special_source_title)
        summary_text.addWidget(self.special_source_meta)
        self.special_solve_button = QPushButton("抓取并智能配方")
        self.special_solve_button.setObjectName("primaryButton")
        self.special_solve_button.setEnabled(False)
        self.special_solve_button.clicked.connect(self._start_special_collection)
        self.special_solve_button.hide()
        summary_top.addLayout(summary_text, 1)
        summary_top.addWidget(self.special_solve_button, 0, Qt.AlignmentFlag.AlignTop)
        summary_layout.addLayout(summary_top)
        self.special_collection_status = QLabel(
            "勾选已登录且支持精确磨损的候选源后开始；默认间隔 3 秒（C5 最短 5 秒）。"
        )
        self.special_collection_status.setObjectName("muted")
        self.special_collection_status.setWordWrap(True)
        summary_layout.addWidget(self.special_collection_status)
        layout.addWidget(summary)
        self._special_collection_host = QVBoxLayout()
        self._special_collection_host.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._special_collection_host)
        self.special_materials_layout = QVBoxLayout()
        self.special_materials_layout.setSpacing(10)
        layout.addLayout(self.special_materials_layout)
        self.special_results_title = QLabel("智能配单结果")
        self.special_results_title.setObjectName("sectionTitle")
        self.special_results_title.hide()
        layout.addWidget(self.special_results_title)
        self.special_results_layout = QVBoxLayout()
        self.special_results_layout.setSpacing(10)
        layout.addLayout(self.special_results_layout)
        return page

    def _set_mode(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(index)
        self._place_collection_controls(index)
        special_running = bool(
            self._special_worker is not None and self._special_worker.isRunning()
        )
        if not self._collection_running and not special_running:
            has_special_materials = any(
                isinstance(item, dict)
                for item in self._special_payload.get("materials", [])
            )
            self.collection_toggle_button.setEnabled(
                index != 2 or has_special_materials
            )
            self.collection_toggle_button.setText("开始采集")
        for button_index, button in enumerate(self.mode_buttons):
            button.setChecked(button_index == index)
            button.setProperty("active", button_index == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _place_collection_controls(self, index: int) -> None:
        for host in (
            self._login_collection_host,
            self._skin_collection_host,
            self._recipe_collection_host,
            self._special_collection_host,
        ):
            host.removeWidget(self.collection_controls_widget)
        if index == 0:
            target = self._skin_collection_host
        elif index == 1:
            target = self._recipe_collection_host
        else:
            target = self._special_collection_host
        target.addWidget(self.collection_controls_widget)
        self.collection_controls_widget.show()

    def _uncheck_candidate_source(self, provider: str) -> None:
        """Clear「候选源」for one platform and persist the preference."""
        source = self._source_checks.get(provider)
        if source is None or not source.isChecked():
            return
        source.blockSignals(True)
        source.setChecked(False)
        source.blockSignals(False)
        self._save_collection_settings()

    def _refresh_login_states(self) -> None:
        steam_ready = steam_session_available()
        for market in MARKETPLACES:
            button = self._login_buttons[market.key]
            checking = False
            if market.key == "steam":
                state = "confirmed" if steam_ready else "missing"
                logged_in = steam_ready
                text = (
                    "● 已检测到 Steam 库存登录会话"
                    if steam_ready
                    else "○ 未检测到登录会话，请前往 Steam 库存登录"
                )
                button.setToolTip("状态来自 Steam 库存管理保存的登录会话")
            elif market.key in APP_LOGIN_PROVIDERS:
                result = self._verified_logins.get(market.key)
                checking = bool(result and result.get("checking"))
                logged_in = bool(result and result.get("ok"))
                if checking:
                    state = "unknown"
                    text = f"… {result.get('message') or '正在处理登录状态'}"
                elif result is None:
                    state = "unknown"
                    text = "○ 尚未校验；系统浏览器登录不等于 APP 已登录"
                elif logged_in:
                    state = "confirmed"
                    account = str(result.get("account_name") or "").strip()
                    text = f"● 已验证登录{f' · {account}' if account else ''}"
                elif result.get("indeterminate"):
                    state = "unknown"
                    text = f"△ {result.get('message') or '暂时无法确认'}"
                else:
                    state = "missing"
                    text = f"○ {result.get('message') or '未登录'}"
                button.setToolTip(
                    "请点击上方「校验登录」确认 APP 登录态。"
                    "各平台校验与采集使用相同在售接口。"
                )
            else:
                logged_in = False
                state = "unknown"
                text = "○ 该平台暂不支持 APP 登录校验"
                button.setToolTip("该平台暂不支持 APP 登录校验")
            button.setText(text)
            button.setProperty("state", state)
            button.style().unpolish(button)
            button.style().polish(button)
            source = self._source_checks.get(market.key)
            if source is not None:
                exact_supported = market.key in EXACT_WEAR_PROVIDERS
                app_login = market.key in APP_LOGIN_PROVIDERS
                if app_login:
                    # Keep勾选 only while login is verified. Uncheck when:
                    # 未登录 / 校验未成功 / （清除登录后也会走到这里）.
                    if logged_in:
                        source.setEnabled(True)
                    else:
                        if not checking:
                            self._uncheck_candidate_source(market.key)
                        source.setEnabled(False)
                else:
                    source.setEnabled(False)
                if market.key == "steam":
                    source.setToolTip("Steam 原生挂单不提供精确磨损，不能作为采集候选源")
                elif not app_login:
                    source.setToolTip("该平台暂不支持 APP 登录校验，不能作为采集候选源")
                elif checking:
                    source.setToolTip("登录处理中，稍后可再勾选候选源")
                elif not logged_in:
                    source.setToolTip("请先「登录 / 打开」并完成校验，再勾选候选源")
                elif exact_supported:
                    source.setToolTip(
                        "勾选后，「开始采集」会抓取该平台精确磨损挂单"
                    )
                else:
                    source.setToolTip(
                        "勾选后，「开始采集」会打开该平台材料链接；"
                        "暂不支持精确磨损挂单导入"
                    )

    def _show_clear_login_menu(self) -> None:
        menu = QMenu(self)
        for provider in ("buff", "yyyp", "c5", "eco"):
            name = provider_display_name(provider)
            menu.addAction(f"清除 {name} 登录信息").triggered.connect(
                lambda _=False, key=provider: self._clear_provider_login(key)
            )
        menu.addSeparator()
        menu.addAction("清除全部平台登录信息").triggered.connect(
            self._clear_all_marketplace_logins
        )
        menu.exec(
            self.clear_login_button.mapToGlobal(
                self.clear_login_button.rect().bottomLeft()
            )
        )

    def _clear_provider_login(self, provider: str, *, confirm: bool = True) -> bool:
        if (
            self._login_capture_worker is not None
            and self._login_capture_worker.isRunning()
        ):
            show_toast(self, "请先关闭或完成当前平台登录窗口", style="warning")
            return False
        name = provider_display_name(provider)
        if confirm:
            answer = QMessageBox.question(
                self,
                f"清除 {name} 登录信息",
                f"将删除本 APP 保存的 {name} 登录凭证和独立浏览器登录目录。\n"
                "不会退出您日常使用的 Edge / Chrome 主浏览器账号。是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        result = clear_provider_auth(provider)
        if not result.get("ok"):
            show_toast(
                self,
                str(result.get("error") or "清除登录信息失败"),
                style="warning",
            )
            return False
        self._verified_logins.pop(provider, None)
        self._login_confirmed.pop(provider, None)
        set_marketplace_login_confirmed(provider, False)
        self._uncheck_candidate_source(provider)
        self._refresh_login_states()
        if result.get("profile_error"):
            show_toast(
                self,
                f"{name} 凭证已清除；浏览器目录正被占用，退出程序后可再次清除",
                style="warning",
            )
        else:
            show_toast(self, f"已清除 {name} 登录信息", style="success")
        return True

    def _clear_all_marketplace_logins(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除全部平台登录信息",
            "将清除 BUFF、悠悠有品、C5GAME、ECOSteam 的 APP 登录凭证"
            "与独立浏览器登录目录。\n"
            "Steam 账号请在“Steam 库存”页面管理。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for provider in sorted(APP_LOGIN_PROVIDERS):
            clear_provider_auth(provider)
        self._reset_login_states()
        show_toast(self, "已清除全部平台登录信息（Steam 除外）", style="success")

    def _reset_login_states(self) -> None:
        self._login_confirmed = {}
        self._verified_logins = {}
        clear_confirmed_marketplace_logins()
        self._refresh_login_states()

    def _validate_marketplace_logins(self) -> None:
        if (
            self._login_validation_worker is not None
            and self._login_validation_worker.isRunning()
        ):
            return
        providers = sorted(APP_LOGIN_PROVIDERS)
        for provider in providers:
            self._verified_logins[provider] = {"checking": True}
        self.login_validate_button.setEnabled(False)
        self.login_validate_button.setText("校验中…")
        self._refresh_login_states()
        worker = MarketplaceLoginValidationWorker(providers, self)
        worker.provider_checked.connect(self._marketplace_login_checked)
        worker.completed.connect(self._marketplace_login_validation_completed)
        worker.finished.connect(worker.deleteLater)
        self._login_validation_worker = worker
        worker.start()

    def _apply_c5_real_login_failure(self) -> None:
        clear_c5_session_auth()
        self._uncheck_candidate_source("c5")
        self._login_confirmed.pop("c5", None)
        set_marketplace_login_confirmed("c5", False)

    def _marketplace_login_checked(self, provider: str, result: object) -> None:
        if isinstance(result, dict):
            self._verified_logins[provider] = dict(result)
        else:
            self._verified_logins[provider] = {
                "ok": False,
                "indeterminate": True,
                "message": "校验返回格式异常",
            }
        state = self._verified_logins[provider]
        # 校验未成功（失败 / 无法确认）→ 取消候选源勾选
        if not state.get("ok"):
            self._uncheck_candidate_source(provider)
        if provider == "c5" and not state.get("ok") and not state.get("indeterminate"):
            self._apply_c5_real_login_failure()
        self._refresh_login_states()

    def _marketplace_login_validation_completed(self) -> None:
        self._login_validation_worker = None
        self.login_validate_button.setEnabled(True)
        self.login_validate_button.setText("校验登录")
        self._refresh_login_states()
        passed = [
            provider
            for provider, result in self._verified_logins.items()
            if result.get("ok")
        ]
        if passed:
            names = [provider_display_name(provider) for provider in passed]
            show_toast(self, f"登录校验成功：{'、'.join(names)}", style="success")
        else:
            show_toast(
                self,
                "未发现可供 APP 使用的有效平台登录，请查看各平台状态说明",
                style="warning",
            )

    def _open_platform_account(self, key: str) -> None:
        if key in APP_LOGIN_PROVIDERS:
            self._start_marketplace_login(key)
            return
        if self.mode_stack.currentIndex() == 0:
            self.open_marketplace(key)
            return
        market = next(item for item in MARKETPLACES if item.key == key)
        QDesktopServices.openUrl(QUrl(market.home_url))

    def _start_marketplace_login(self, provider: str) -> None:
        if (
            self._login_capture_worker is not None
            and self._login_capture_worker.isRunning()
        ):
            show_toast(self, "请先完成当前平台登录窗口", style="warning")
            return
        # Avoid two Chromium windows fighting over the same market profile.
        try:
            from core.market_access_session import close_access_sessions
            from core.c5_browser_collect import close_c5_browser_collector

            close_access_sessions(provider)
            if provider == "c5":
                close_c5_browser_collector()
        except Exception:
            pass
        market = next(item for item in MARKETPLACES if item.key == provider)
        self._verified_logins[provider] = {
            "checking": True,
            "message": f"等待在登录窗口完成 {market.name} 登录",
        }
        self._market_open_buttons[provider].setEnabled(False)
        self._market_open_buttons[provider].setText("启动中…")
        self._refresh_login_states()
        worker = MarketplaceLoginCaptureWorker(provider, self)
        worker.progress.connect(self._marketplace_login_progress)
        worker.completed.connect(self._marketplace_login_captured)
        worker.finished.connect(worker.deleteLater)
        self._login_capture_worker = worker
        worker.start()

    def _marketplace_login_progress(self, provider: str, message: str) -> None:
        self._verified_logins[provider] = {
            "checking": True,
            "message": message,
        }
        button = self._market_open_buttons.get(provider)
        if button is not None and not button.isEnabled():
            if "启动" in message:
                button.setText("启动中…")
            else:
                button.setText("等待登录…")
        self._refresh_login_states()

    def _marketplace_login_captured(self, provider: str, result: object) -> None:
        self._login_capture_worker = None
        self._market_open_buttons[provider].setEnabled(True)
        self._market_open_buttons[provider].setText("登录 / 打开")
        if isinstance(result, dict):
            self._verified_logins[provider] = dict(result)
        else:
            self._verified_logins[provider] = {
                "ok": False,
                "indeterminate": True,
                "message": "登录捕获返回格式异常",
            }
        self._refresh_login_states()
        state = self._verified_logins[provider]
        if not state.get("ok"):
            self._uncheck_candidate_source(provider)
        if provider == "c5" and not state.get("ok") and not state.get("indeterminate"):
            self._apply_c5_real_login_failure()
            self._refresh_login_states()
            state = self._verified_logins[provider]
        if state.get("ok"):
            show_toast(self, "平台登录成功，凭证已安全保存", style="success")
        else:
            show_toast(
                self,
                str(state.get("message") or "平台登录未完成"),
                style="warning",
            )

    def _template(self):
        return get_name_map().get(normalize_name(self.name_edit.text()))

    def _sync_hint(self) -> None:
        if not self.name_edit.text().strip():
            self.hint.setText("未选择饰品时，按钮打开平台市场首页。")
        elif self._template() is None:
            self.hint.setText("请从候选列表中选择完整饰品名称。")
        else:
            low, high = self.single_range_selector.selected_range()
            self.hint.setText(
                f"已生成当前饰品 {low:g}–{high:g} 区间的平台直达链接。"
            )

    def _on_single_skin_changed(self) -> None:
        if not self._collection_running:
            self._collected_items = []
            self.collection_import_button.hide()
            self.collection_save_json_button.hide()
            self._set_collection_status_text(
                "选择饰品和磨损区间后开始；完成后可导入计算或保存为 JSON"
            )
        template = self._template()
        if template is None:
            self.single_range_selector.setEnabled(False)
            self.single_range_selector.set_wear_bounds(0.0, 1.0)
            self.single_range_label.setText("选择饰品后可拖动左右手柄")
        else:
            self.single_range_selector.setEnabled(True)
            self.single_range_selector.set_wear_bounds(
                float(template.min_float),
                float(template.max_float),
            )
            low, high = self.single_range_selector.selected_range()
            self.single_range_label.setText(f"{low:g} ～ {high:g}")
        self._sync_hint()

    def _on_single_range_changed(self, low: float, high: float) -> None:
        self.single_range_label.setText(f"{low:g} ～ {high:g}")
        self._sync_hint()

    def _single_wear_and_range(self) -> tuple[str, float, float]:
        low, high = self.single_range_selector.selected_range()
        appearance = SkinInstance.get_appearance((low + high) / 2) or ""
        return appearance, low, high

    def open_marketplace(self, key: str) -> None:
        template = self._template()
        if template is None:
            marketplace = next(m for m in MARKETPLACES if m.key == key)
            url = marketplace.home_url
        else:
            appearance, low, high = self._single_wear_and_range()
            url = links_for_template(
                template,
                appearance,
                min_wear=low,
                max_wear=high,
            ).get(key, "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _load_recipe(self) -> None:
        if self._recipe_thread is not None and self._recipe_thread.isRunning():
            return
        reference = self.recipe_edit.text().strip()
        if not reference:
            self._set_recipe_load_emphasized(True)
            self.recipe_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            show_toast(self, "请先粘贴 CS2TH 配方链接", style="warning")
            return
        session = AuthClient().load_local_session()
        token = session.access_token if session is not None else ""
        self._set_recipe_load_emphasized(False)
        self.recipe_load_button.setEnabled(False)
        self.recipe_load_button.setText("读取中…")
        self.recipe_status.setText(
            "正在连接 CS2TH 并读取配方材料"
            + ("与备选材料…" if self.include_alternatives.isChecked() else "…")
        )
        thread = RecipeLoadThread(
            reference,
            token,
            self,
            include_alternatives=self.include_alternatives.isChecked(),
        )
        thread.completed.connect(self._recipe_loaded)
        thread.finished.connect(thread.deleteLater)
        self._recipe_thread = thread
        thread.start()

    def load_recipe_reference(self, reference: str) -> None:
        """Open recipe collection mode and load a CS2TH recipe link."""
        self._set_mode(1)
        self.recipe_edit.setText(str(reference or "").strip())
        self._load_recipe()

    def _on_include_alternatives_toggled(self, *_args) -> None:
        self._save_collection_settings()
        if self._last_recipe_payload is not None:
            # Re-fetch so alternatives are loaded when newly enabled.
            if self.include_alternatives.isChecked() and not isinstance(
                self._last_recipe_payload.get("_alternatives_by_input"),
                dict,
            ):
                if self.recipe_edit.text().strip():
                    self._load_recipe()
                else:
                    self._start_attach_alternatives(self._last_recipe_payload)
                return
            self._render_loaded_recipe_materials(self._last_recipe_payload)

    def _start_attach_alternatives(self, payload: dict) -> None:
        if self._recipe_thread is not None and self._recipe_thread.isRunning():
            return
        session = AuthClient().load_local_session()
        token = session.access_token if session is not None else ""
        self.recipe_status.setText("正在拉取备选材料…")
        thread = RecipeAlternativesThread(payload, token, self)
        thread.completed.connect(self._alternatives_attached)
        thread.finished.connect(thread.deleteLater)
        self._recipe_thread = thread
        thread.start()

    def _alternatives_attached(self, payload: object, error: str) -> None:
        self._recipe_thread = None
        if error or not isinstance(payload, dict):
            if self._last_recipe_payload is not None:
                self._render_loaded_recipe_materials(self._last_recipe_payload)
            show_toast(self, error or "备选材料读取失败", style="warning")
            return
        self._apply_recipe_payload(payload)
        if not self.recipe_edit.text().strip():
            self.recipe_summary_meta.setText("来自配方管理 · 可调整磨损采集")
            self.open_recipe_button.hide()

    def _apply_recipe_payload(self, payload: dict) -> None:
        self._last_recipe_payload = dict(payload)
        materials = [item for item in payload.get("inputs", []) if isinstance(item, dict)]
        alt_map = payload.get("_alternatives_by_input")
        alt_count = 0
        if isinstance(alt_map, dict):
            alt_count = sum(
                len(items) for items in alt_map.values() if isinstance(items, list)
            )
        status = (
            "已读取配方。默认展开每条材料磨损档的前一档与后一档，可拖动调整；"
            "平台采集请勾选上方候选源后点「开始采集」；"
            "采集完成后可选择导入计算或保存为 JSON。"
        )
        if self.include_alternatives.isChecked():
            status += f" 已附带 {alt_count} 条备选材料。"
        self.recipe_status.setText(status)
        self._render_loaded_recipe_materials(payload)
        if not materials:
            show_toast(self, "配方中没有可采集材料", style="warning")

    def _recipe_loaded(self, payload: object, error: str) -> None:
        self.recipe_load_button.setEnabled(True)
        self.recipe_load_button.setText("读取配方")
        self._set_recipe_load_emphasized(False)
        self._recipe_thread = None
        if error or not isinstance(payload, dict):
            self.recipe_status.setText(error or "配方读取失败")
            show_toast(self, error or "配方读取失败", style="warning")
            self._sync_recipe_materials_empty_state()
            return
        self._last_recipe_payload = dict(payload)
        materials = [item for item in payload.get("inputs", []) if isinstance(item, dict)]
        alt_map = payload.get("_alternatives_by_input")
        alt_count = 0
        if isinstance(alt_map, dict):
            alt_count = sum(
                len(items) for items in alt_map.values() if isinstance(items, list)
            )
        status = (
            "已读取配方。默认展开每条材料磨损档的前一档与后一档，可拖动调整；"
            "平台采集请勾选上方候选源后点「开始采集」；"
            "采集完成后可选择导入计算或保存为 JSON。"
        )
        if self.include_alternatives.isChecked():
            status += f" 已附带 {alt_count} 条备选材料。"
        self.recipe_status.setText(status)
        recipe_id = str(payload.get("_recipe_id") or "")
        self._current_recipe_url = (
            f"{AUTH_API_BASE_URL}/recipe/{recipe_id}?market={payload.get('_market') or 'spot'}"
        )
        market = "现货" if payload.get("_market") == "spot" else "期货"
        self.recipe_summary_title.setText(
            f"{payload.get('collection_name') or 'CS2TH 配方'} · {payload.get('input_rarity') or ''}"
        )
        cost = float(payload.get("input_cost") or 0)
        roi = float(payload.get("roi") or 0) * 100
        self.recipe_summary_meta.setText(
            f"{market} · #{recipe_id[:8]} · 成本 ¥{cost:.2f} · 收益率 {roi:.2f}%"
        )
        self.recipe_summary.show()
        self.open_recipe_button.show()
        self._render_loaded_recipe_materials(payload)
        if not materials:
            show_toast(self, "配方中没有可采集材料", style="warning")

    def _iter_recipe_display_materials(
        self,
        payload: dict,
    ) -> list[tuple[dict, bool]]:
        """Return (material, is_alternative) rows for the current toggle state."""
        inputs = [item for item in payload.get("inputs", []) if isinstance(item, dict)]
        rows: list[tuple[dict, bool]] = []
        alt_map = payload.get("_alternatives_by_input")
        show_alts = self.include_alternatives.isChecked() and isinstance(alt_map, dict)
        for index, material in enumerate(inputs):
            rows.append((material, False))
            if not show_alts:
                continue
            alts = alt_map.get(index, alt_map.get(str(index), []))
            if not isinstance(alts, list):
                continue
            for alt in alts:
                if isinstance(alt, dict):
                    rows.append((alt, True))
        return rows

    def _render_loaded_recipe_materials(self, payload: dict) -> None:
        rows = self._iter_recipe_display_materials(payload)
        primary_count = sum(1 for _item, is_alt in rows if not is_alt)
        alt_count = sum(1 for _item, is_alt in rows if is_alt)
        piece_count = sum(
            int(item.get("count") or 0)
            for item, is_alt in rows
            if not is_alt and isinstance(item, dict)
        )
        count_text = f"{primary_count} 种材料 · 共 {piece_count} 件"
        if alt_count:
            count_text += f" · 备选 {alt_count}"
        self.material_count.setText(count_text)
        _clear_layout(self.materials_layout)
        self._recipe_material_states = []
        self._reset_collection_links()
        for index, (material, is_alt) in enumerate(rows, start=1):
            self.materials_layout.addWidget(
                self._material_card(index, material, is_alternative=is_alt)
            )
        self._rebuild_recipe_collection_links()
        self._sync_recipe_materials_empty_state()

    def _open_current_recipe(self) -> None:
        url = getattr(self, "_current_recipe_url", "")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _recipe_material_default_range(
        self,
        material: dict,
        *,
        total_min: float,
        total_max: float,
    ) -> tuple[float, float, str, float | None, float | None, float | None]:
        min_wear, max_wear, wear_label = material_wear_range(material)
        if min_wear is None and material.get("min_wear") is not None:
            try:
                min_wear = float(material.get("min_wear"))
                max_wear = float(material.get("max_wear"))
                wear_label = f"{min_wear:g} ～ {max_wear:g}"
            except (TypeError, ValueError):
                min_wear, max_wear = None, None
        recipe_min = float(min_wear) if min_wear is not None else None
        recipe_max = float(max_wear) if max_wear is not None else None
        marker: float | None = None
        if material.get("unit_float") is not None:
            try:
                marker = float(material.get("unit_float") or 0)
            except (TypeError, ValueError):
                marker = None
        if marker is None and material.get("wear_value") is not None:
            try:
                marker = float(material.get("wear_value") or 0)
            except (TypeError, ValueError):
                marker = None

        # Collection presets already store the exact purchase band — do not
        # shrink/expand via neighboring MID buckets around the midpoint.
        if material.get("exact_collection_range") and (
            recipe_min is not None and recipe_max is not None
        ):
            low = max(total_min, min(total_max, float(recipe_min)))
            high = max(total_min, min(total_max, float(recipe_max)))
            if high < low:
                low, high = high, low
            return low, high, wear_label, low, high, marker

        if recipe_min is not None and recipe_max is not None:
            mid = (recipe_min + recipe_max) / 2.0
            if marker is None:
                marker = mid
        elif recipe_max is not None:
            mid = recipe_max
            if marker is None:
                marker = mid
        elif marker is not None:
            mid = marker
        else:
            mid = (total_min + total_max) / 2.0
        low, high = neighboring_purchase_interval(
            mid,
            min_float=total_min,
            max_float=total_max,
        )
        return low, high, wear_label, recipe_min, recipe_max, marker

    def _material_card(
        self,
        index: int,
        material: dict,
        *,
        is_alternative: bool = False,
    ) -> QFrame:
        card = QFrame()
        card.setObjectName(
            "recipeBridgeMaterialAlt" if is_alternative else "recipeBridgeMaterial"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)

        top = QHBoxLayout()
        count = int(material.get("count") or 0)
        name = str(material.get("name") or material.get("market_hash_name") or "未知饰品")
        if is_alternative:
            title = QLabel(f"{index:02d}  备选 · {name}")
            title.setObjectName("recipeBridgeMaterialAltTitle")
        else:
            title = QLabel(f"{index:02d}  {name}")
            title.setObjectName("recipeBridgeMaterialTitle")
        title.setWordWrap(True)
        if is_alternative:
            count_label = QLabel("备选")
            count_label.setObjectName("recipeBridgeAltBadge")
        else:
            count_label = QLabel(f"× {count}")
            count_label.setObjectName("recipeBridgeCount")
        top.addWidget(title, 1)
        top.addWidget(count_label)
        layout.addLayout(top)

        template = get_name_map().get(normalize_name(name))
        if template is None:
            total_min, total_max = 0.0, 1.0
        else:
            total_min = float(template.min_float)
            total_max = float(template.max_float)
        (
            selected_min,
            selected_max,
            recipe_wear_label,
            recipe_min,
            recipe_max,
            recipe_marker,
        ) = self._recipe_material_default_range(
            material,
            total_min=total_min,
            total_max=total_max,
        )

        details = QHBoxLayout()
        wear = QLabel(f"采集磨损  {selected_min:g} ～ {selected_max:g}")
        wear.setObjectName("recipeBridgeWear")
        unit_price = float(material.get("unit_price_cny") or 0)
        price_bits = [str(material.get("wear") or "").strip()]
        range_prefix = (
            "预设" if material.get("exact_collection_range") else "配方"
        )
        if recipe_wear_label:
            price_bits.append(f"{range_prefix} {recipe_wear_label}")
        # Preset bands have no single "target float"; skip the midpoint label.
        if recipe_marker is not None and not material.get("exact_collection_range"):
            price_bits.append(f"对应 {recipe_marker:g}")
        if unit_price > 0:
            price_bits.append(f"单价 ¥{unit_price:.2f}")
        if is_alternative and material.get("supports_wear") is False:
            price_bits.append("可能不支持该磨损档")
        price = QLabel(" · ".join(bit for bit in price_bits if bit))
        price.setObjectName("muted")
        details.addWidget(wear)
        details.addStretch(1)
        details.addWidget(price)
        layout.addLayout(details)

        selector = WearRangeSelector()
        selector.setEnabled(True)
        selector.set_wear_bounds(
            total_min,
            total_max,
            selected_min=selected_min,
            selected_max=selected_max,
        )
        if material.get("exact_collection_range"):
            # Preset band is already the selected handles; skip "配方" overlay.
            selector.set_recipe_annotation()
        else:
            selector.set_recipe_annotation(
                recipe_min=recipe_min,
                recipe_max=recipe_max,
                marker=recipe_marker,
            )
        layout.addWidget(selector)

        state = {
            "name": name,
            "material": material,
            "template": template,
            "selector": selector,
            "wear_label": wear,
            "is_alternative": is_alternative,
        }
        self._recipe_material_states.append(state)
        selector.rangeChanged.connect(
            lambda _low, _high, current=state: self._on_recipe_material_range_changed(
                current
            )
        )
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return card

    def _on_recipe_material_range_changed(self, state: dict) -> None:
        selector: WearRangeSelector = state["selector"]
        low, high = selector.selected_range()
        state["wear_label"].setText(f"采集磨损  {low:g} ～ {high:g}")
        self._rebuild_recipe_collection_links()

    def _recipe_material_links(
        self,
        state: dict,
    ) -> tuple[dict[str, str], str, float, str]:
        selector: WearRangeSelector = state["selector"]
        low, high = selector.selected_range()
        wear_label = f"{low:g} ～ {high:g}"
        template = state.get("template")
        name = str(state.get("name") or "")
        material = state.get("material") if isinstance(state.get("material"), dict) else {}
        if template is None:
            links = links_for_recipe_material(
                material,
                min_wear=low,
                max_wear=high,
            )
        else:
            appearance = SkinInstance.get_appearance((low + high) / 2) or ""
            links = links_for_template(
                template,
                appearance,
                min_wear=low,
                max_wear=high,
            )
            recipe_links = links_for_recipe_material(
                material,
                min_wear=low,
                max_wear=high,
            )
            for key, url in recipe_links.items():
                market = next(item for item in MARKETPLACES if item.key == key)
                if links.get(key) in {"", market.home_url} and url not in {
                    "",
                    market.home_url,
                }:
                    links[key] = url
        return links, wear_label, high, name

    def _rebuild_recipe_collection_links(self) -> None:
        self._reset_collection_links()
        for state in self._recipe_material_states:
            links, _wear_label, _high, name = self._recipe_material_links(state)
            for market in MARKETPLACES:
                direct = links.get(market.key, "")
                if direct and direct != market.home_url:
                    self._collection_links[market.key].append((name, direct))

    def _platform_action_grid(
        self,
        links: dict[str, str],
        wear_label: str,
        max_wear: float | None,
        material_name: str,
        *,
        record_links: bool = True,
    ) -> QGridLayout:
        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        for position, market in enumerate(MARKETPLACES):
            button = QPushButton(market.name)
            button.setObjectName("recipeBridgePlatformButton")
            direct = links.get(market.key, "")
            is_direct = bool(direct and direct != market.home_url)
            button.setEnabled(is_direct)
            if is_direct:
                if record_links:
                    self._collection_links[market.key].append((material_name, direct))
                button.clicked.connect(
                    lambda _=False, url=direct: QDesktopServices.openUrl(QUrl(url))
                )
                if market.key in {"buff", "c5"} and max_wear is not None:
                    button.setToolTip(f"打开商品并带入磨损条件：{wear_label}")
                else:
                    button.setToolTip(f"打开商品；进入平台后核对磨损区间：{wear_label}")
            else:
                button.setToolTip("当前材料缺少该平台的商品 ID")
            actions.addWidget(button, position // 5, position % 5)
        for column in range(5):
            actions.setColumnStretch(column, 1)
        return actions

    def show_special_wear_materials(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._special_payload = dict(payload)
        self._special_slot_count = (
            5 if int(payload.get("slot_count") or 10) == 5 else 10
        )
        materials = [
            material
            for material in payload.get("materials", [])
            if isinstance(material, dict)
        ]
        target = str(payload.get("target_name") or "特殊磨损目标")
        target_low = float(payload.get("target_min_wear") or 0)
        target_high = float(payload.get("target_max_wear") or 0)
        self.special_source_title.setText(f"{target} · 特殊磨损材料")
        self.special_source_meta.setText(
            f"目标产物区间 {target_low:.18f} ～ {target_high:.18f}；"
            f"共找到 {len(materials)} 种可用材料。采购区间用于抓取候选，"
            f"程序会按{self._special_slot_count}件真实磨损平均值重新验算。"
        )
        self.special_collection_status.setText(
            "可选精确候选源：BUFF、悠悠、C5、ECO；Steam 原生挂单不提供精确磨损；"
            "智能配单强制至少 3 秒间隔（C5 至少 5 秒）、结果缓存 3 分钟。"
        )
        self._collected_items = []
        self.collection_import_button.hide()
        self.collection_save_json_button.hide()
        self.collection_toggle_button.setEnabled(bool(materials))
        self.collection_toggle_button.setText("开始采集")
        self._set_collection_status_text(
            "勾选已登录的候选源后开始；完成后可导入计算或保存为 JSON"
        )
        self.special_solve_button.setEnabled(bool(materials))
        self.special_solve_button.setText(
            f"抓取并智能配{self._special_slot_count}件"
        )
        self.special_results_title.hide()
        _clear_layout(self.special_results_layout)
        _clear_layout(self.special_materials_layout)
        self._reset_collection_links()
        for index, material in enumerate(materials, start=1):
            self.special_materials_layout.addWidget(
                self._special_material_card(index, material)
            )
        self._set_mode(2)

    def _selected_special_sources(self) -> list[str]:
        return [
            key
            for key, checkbox in self._source_checks.items()
            if key in EXACT_WEAR_PROVIDERS
            and checkbox.isEnabled()
            and checkbox.isChecked()
        ]

    def _start_special_collection(self) -> None:
        if self._special_worker is not None and self._special_worker.isRunning():
            show_toast(self, "特殊磨损材料正在采集中", style="info")
            return
        providers = self._selected_special_sources()
        if not providers:
            show_toast(
                self,
                "请至少勾选一个已确认登录的精确候选源（BUFF / 悠悠 / C5 / ECO）",
                style="warning",
            )
            return
        materials = [
            item
            for item in self._special_payload.get("materials", [])
            if isinstance(item, dict)
        ]
        target_paint_index = str(
            self._special_payload.get("target_paint_index") or ""
        )
        if not target_paint_index:
            target = get_name_map().get(
                normalize_name(str(self._special_payload.get("target_name") or ""))
            )
            target_paint_index = str(target.paint_index) if target is not None else ""
        if not materials or not target_paint_index:
            show_toast(self, "特殊磨损目标数据不完整，请重新查询", style="warning")
            return
        self.special_solve_button.setEnabled(False)
        self.special_solve_button.setText("正在抓取…")
        self._special_stopping = False
        self.collection_toggle_button.setEnabled(True)
        self.collection_toggle_button.setText("停止采集")
        self.collection_import_button.hide()
        self.collection_save_json_button.hide()
        self._collected_items = []
        self._collection_started_at = time.monotonic()
        self._set_collection_status_text("正在抓取特殊磨损候选并智能配方…")
        self.special_results_title.hide()
        _clear_layout(self.special_results_layout)
        worker = SpecialCollectionWorker(
            materials=materials,
            providers=providers,
            provider_intervals={
                key: self._collection_intervals[key].value() for key in providers
            },
            target_paint_index=target_paint_index,
            target_wear_low=float(
                self._special_payload.get("target_min_wear") or 0
            ),
            target_wear_high=float(
                self._special_payload.get("target_max_wear") or 0
            ),
            slot_count=self._special_slot_count,
            parent=self,
        )
        worker.progress.connect(self.special_collection_status.setText)
        worker.progress.connect(self._set_collection_status_text)
        worker.completed.connect(self._special_collection_completed)
        worker.finished.connect(worker.deleteLater)
        self._special_worker = worker
        worker.start()

    def _special_collection_completed(
        self,
        candidates: object,
        recipes: object,
        message: str,
    ) -> None:
        self._special_worker = None
        was_stopping = self._special_stopping or "已停止" in str(message or "")
        self._special_stopping = False
        self.special_solve_button.setEnabled(True)
        self.special_solve_button.setText(
            f"重新抓取并智能配{self._special_slot_count}件"
        )
        self.collection_toggle_button.setEnabled(True)
        self.collection_toggle_button.setText("开始采集")
        candidate_rows = candidates if isinstance(candidates, list) else []
        recipe_rows = recipes if isinstance(recipes, list) else []
        started_at = self._collection_started_at
        self._collection_started_at = None
        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        if was_stopping:
            kept = [
                dict(item) for item in candidate_rows if isinstance(item, dict)
            ]
            self._apply_stopped_collection_results(
                kept,
                message=str(message or "").strip(),
                elapsed=elapsed,
            )
            status = self.collection_status.text()
            self.special_collection_status.setText(
                status if kept else "采集已停止（无已抓取挂单）"
            )
            return
        self._collected_items = [
            dict(item) for item in candidate_rows if isinstance(item, dict)
        ]
        self._set_collection_status_text(
            f"采集完成 · {format_collection_platform_counts(self._collected_items)}，"
            f"共计 {elapsed:.1f} 秒",
            state="complete",
        )
        if self._collected_items:
            self.collection_import_button.show()
            self.collection_save_json_button.show()
        from collections import Counter

        counts = Counter(
            str(item.get("platform") or "")
            for item in candidate_rows
            if isinstance(item, dict)
        )
        source_text = "、".join(
            f"{key.upper()} {value}件" for key, value in counts.items()
        )
        if not recipe_rows:
            self.special_collection_status.setText(
                f"候选池 {len(candidate_rows)} 件"
                + (f"（{source_text}）" if source_text else "")
                + (
                    f"；{message or f'未找到能命中特殊磨损的{self._special_slot_count}件组合'}"
                )
            )
            show_toast(
                self,
                f"没有找到可用的{self._special_slot_count}件组合",
                style="warning",
            )
            return
        self.special_collection_status.setText(
            f"候选池 {len(candidate_rows)} 件"
            + (f"（{source_text}）" if source_text else "")
            + f"；找到 {len(recipe_rows)} 组可购买方案。"
            + (f" 部分来源提示：{message}" if message else "")
        )
        self.special_results_title.setText(
            f"智能配单结果 · {len(recipe_rows)} 组"
        )
        self.special_results_title.show()
        for index, recipe in enumerate(recipe_rows, start=1):
            if isinstance(recipe, dict):
                self.special_results_layout.addWidget(
                    self._special_solution_card(index, recipe)
                )
        show_toast(
            self,
            f"已找到具体{self._special_slot_count}件材料组合",
            style="success",
        )

    def _special_solution_card(self, index: int, recipe: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("recipeBridgeMaterial")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(8)
        top = QHBoxLayout()
        substrates = [
            item
            for item in recipe.get("substrates_display") or []
            if isinstance(item, dict)
        ]
        slot_count = len(substrates)
        cost = float(recipe.get("cost") or 0)
        avg_nfv = float(recipe.get("avg_nfv") or 0)
        target_paint_index = str(
            self._special_payload.get("target_paint_index") or ""
        )
        target = get_pid_map().get(target_paint_index)
        output_wear = (
            target.normalized_to_float(avg_nfv, target.min_float, target.max_float)
            if target is not None
            else 0.0
        )
        title = QLabel(
            f"方案 {index:02d} · 共{slot_count}件 · 总价 ¥{cost:.2f}"
        )
        title.setObjectName("recipeBridgeMaterialTitle")
        predicted = QLabel(f"预计产物磨损 {output_wear:.18f}")
        predicted.setObjectName("recipeBridgeWear")
        simulate = QPushButton("导入模拟")
        simulate.setObjectName("primaryButton")
        simulate.setToolTip(
            f"把本方案的{slot_count}件真实磨损材料导入炼金模拟，查看所有产物磨损"
        )
        simulate.clicked.connect(
            lambda _=False, value=dict(recipe): self._import_special_solution_to_simulation(
                value
            )
        )
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(predicted)
        top.addWidget(simulate)
        layout.addLayout(top)

        platform_names = {market.key: market.name for market in MARKETPLACES}
        for row_index, substrate in enumerate(
            substrates,
            start=1,
        ):
            if not isinstance(substrate, dict):
                continue
            row = QHBoxLayout()
            name = QLabel(
                f"{row_index:02d}  {substrate.get('name') or '未知材料'}"
            )
            name.setMinimumWidth(0)
            name.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            wear = QLabel(f"{float(substrate.get('float_value') or 0):.18f}")
            wear.setObjectName("muted")
            platform = str(substrate.get("platform") or "")
            price = QLabel(
                f"{platform_names.get(platform, platform.upper())} · "
                f"¥{float(substrate.get('price') or 0):.2f}"
            )
            price.setObjectName("muted")
            buy = QPushButton("打开购买")
            buy.setObjectName("recipeBridgePlatformButton")
            url = str(substrate.get("purchase_link") or "")
            buy.setEnabled(bool(url))
            if url:
                buy.clicked.connect(
                    lambda _=False, value=url: QDesktopServices.openUrl(QUrl(value))
                )
            row.addWidget(name, 1)
            row.addWidget(wear)
            row.addWidget(price)
            row.addWidget(buy)
            layout.addLayout(row)
        return card

    def _import_special_solution_to_simulation(self, recipe: dict) -> None:
        substrates = [
            dict(item)
            for item in recipe.get("substrates_display") or []
            if isinstance(item, dict)
        ]
        slot_count = len(substrates)
        if slot_count not in (5, 10):
            show_toast(
                self,
                f"该方案包含{slot_count}件材料，仅支持5合一或10合一模拟",
                style="warning",
            )
            return
        payload = dict(recipe)
        payload["substrates_display"] = substrates
        payload["simulation_slot_count"] = slot_count
        self.import_to_simulation_requested.emit(payload)

    def _choose_saved_recipe(self) -> None:
        entries = list_saved_recipes()
        if not entries:
            show_toast(self, "配方管理中还没有保存的配方", style="warning")
            return
        labels: list[str] = []
        for _path, payload in entries:
            title = str(payload.get("title") or "未命名配方").strip()
            saved_at = str(payload.get("saved_at") or "")[:19].replace("T", " ")
            labels.append(f"{title} · {saved_at}" if saved_at else title)
        selected, accepted = QInputDialog.getItem(
            self,
            "从配方管理导入",
            "选择配方：",
            labels,
            0,
            False,
        )
        if not accepted:
            return
        index = labels.index(selected)
        payload = entries[index][1]
        recipe = payload.get("recipe")
        error = self.show_saved_recipe_materials(
            recipe,
            title=str(payload.get("title") or "未命名配方"),
        )
        if error:
            show_toast(self, error, style="warning")
        else:
            show_toast(self, "已从配方管理导入材料", style="success")

    def show_saved_recipe_materials(
        self,
        recipe: object,
        *,
        title: str = "",
    ) -> str | None:
        """Render a saved recipe in recipe mode, using only each wear's own bucket."""
        if not isinstance(recipe, dict):
            return "配方数据无效"
        display_title = title.strip() or "保存配方"
        payload = saved_recipe_to_bridge_payload(recipe, title=display_title)
        inputs = [
            item for item in payload.get("inputs", []) if isinstance(item, dict)
        ]
        if not inputs:
            return "配方中没有可采集材料"
        self.recipe_edit.clear()
        self._current_recipe_url = ""
        self._set_mode(1)
        if self.include_alternatives.isChecked():
            self._last_recipe_payload = payload
            self.recipe_summary_title.setText(display_title)
            self.recipe_summary_meta.setText("来自配方管理 · 正在拉取备选材料…")
            self.recipe_summary.show()
            self.open_recipe_button.hide()
            # Show primaries immediately, then enrich with alternatives.
            self._render_loaded_recipe_materials(payload)
            self._start_attach_alternatives(payload)
            return None
        self._apply_recipe_payload(payload)
        self.recipe_summary_title.setText(display_title)
        self.recipe_summary_meta.setText("来自配方管理 · 可调整磨损采集")
        self.recipe_summary.show()
        self.open_recipe_button.hide()
        return None

    def show_collection_preset_materials(
        self,
        items: object,
        *,
        title: str = "",
    ) -> str | None:
        """Load a user collection preset into recipe-collection mode."""
        if not isinstance(items, list):
            return "采集预设无效"
        inputs: list[dict] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            try:
                min_wear = float(raw.get("min_wear"))
                max_wear = float(raw.get("max_wear"))
            except (TypeError, ValueError):
                continue
            if min_wear > max_wear:
                min_wear, max_wear = max_wear, min_wear
            inputs.append(
                {
                    "name": name,
                    "count": 1,
                    "min_wear": min_wear,
                    "max_wear": max_wear,
                    # Keep the preset band as-is (no neighboring-bucket expand).
                    "exact_collection_range": True,
                }
            )
        if not inputs:
            return "采集预设中没有可采集材料"
        display_title = str(title or "").strip() or "采集预设"
        payload = {
            "inputs": inputs,
            "collection_name": display_title,
            "input_rarity": "",
            "input_cost": 0,
            "roi": 0,
            "_market": "spot",
            "_recipe_id": "",
        }
        self.recipe_edit.clear()
        self._current_recipe_url = ""
        self._set_mode(1)
        self._last_recipe_payload = payload
        self.recipe_status.setText(
            "已导入采集预设。可拖动调整每种材料的磨损区间；"
            "勾选候选源后点「开始采集」。"
        )
        self.recipe_summary_title.setText(display_title)
        self.recipe_summary_meta.setText(
            f"来自采集预设 · {len(inputs)} 种材料 · 可调整磨损后采集"
        )
        self.recipe_summary.show()
        self.open_recipe_button.hide()
        self._render_loaded_recipe_materials(payload)
        return None

    def _special_material_card(self, index: int, material: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("recipeBridgeMaterial")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)
        name = str(material.get("name") or "未知材料")
        count = max(1, int(material.get("count") or 1))
        title = QLabel(f"{index:02d}  {name}  × {count}")
        title.setObjectName("recipeBridgeMaterialTitle")
        layout.addWidget(title)

        wear_value = float(material.get("wear_value") or 0)
        min_wear = float(material.get("min_wear") or wear_value)
        max_wear = float(material.get("max_wear") or wear_value)
        wear_label = f"{min_wear:g} ～ {max_wear:g}"
        details = QHBoxLayout()
        range_label = QLabel(f"可采购磨损区间  {wear_label}")
        range_label.setObjectName("recipeBridgeWear")
        point = QLabel(f"对应磨损 {wear_value:.18f}")
        point.setObjectName("muted")
        details.addWidget(range_label)
        details.addStretch(1)
        details.addWidget(point)
        layout.addLayout(details)

        template = get_name_map().get(normalize_name(name))
        if template is None:
            links = {market.key: market.home_url for market in MARKETPLACES}
            total_min, total_max = 0.0, 1.0
        else:
            total_min, total_max = template.min_float, template.max_float
            appearance = SkinInstance.get_appearance(wear_value) or ""
            links = links_for_template(
                template,
                appearance,
                min_wear=min_wear,
                max_wear=max_wear,
            )
        layout.addWidget(
            WearIntervalBar(
                total_min=total_min,
                total_max=total_max,
                selected_min=min_wear,
                selected_max=max_wear,
                marker=wear_value,
            )
        )
        layout.addLayout(
            self._platform_action_grid(links, wear_label, max_wear, name)
        )
        return card

    def _reset_collection_links(self) -> None:
        self._collection_links = {market.key: [] for market in MARKETPLACES}

    def _single_skin_collection_link(self, key: str) -> tuple[str, str] | None:
        template = self._template()
        if template is None:
            return None
        appearance, low, high = self._single_wear_and_range()
        links = links_for_template(
            template,
            appearance,
            min_wear=low,
            max_wear=high,
        )
        market = next(item for item in MARKETPLACES if item.key == key)
        url = links.get(key, "")
        if not url or url == market.home_url:
            return None
        return self.name_edit.text().strip(), url

    def _collection_finished_idle(self) -> bool:
        """True when a prior run finished and the start button would begin a new one."""
        if self._collection_running:
            return False
        state = str(self.collection_status.property("collectionState") or "")
        return state == "complete" or bool(self._collected_items)

    def _confirm_restart_collection(self) -> bool:
        count = len(self._collected_items)
        detail = (
            f"当前已有采集结果（{count} 条）。\n重新采集会覆盖本次结果，确定继续吗？"
            if count
            else "当前采集已结束。\n确定要重新开始采集吗？"
        )
        return ask_confirmation(self, "重新开始采集", detail)

    def _toggle_collection(self) -> None:
        if self.mode_stack.currentIndex() == 2:
            if self._special_worker is not None and self._special_worker.isRunning():
                self._stop_special_collection()
            else:
                if self._collection_finished_idle() and not self._confirm_restart_collection():
                    return
                self._start_special_collection()
            return
        if self._collection_running:
            self._stop_collection()
        else:
            if self._collection_finished_idle() and not self._confirm_restart_collection():
                return
            self._start_collection()

    def _selected_collection_platforms(self) -> list[str]:
        return [
            market.key
            for market in MARKETPLACES
            if self._source_checks.get(market.key) is not None
            and self._source_checks[market.key].isChecked()
            and self._source_checks[market.key].isEnabled()
        ]

    def _collection_items_for_platform(self, key: str) -> list[tuple[str, str]]:
        if self.mode_stack.currentIndex() == 0:
            single = self._single_skin_collection_link(key)
            return [single] if single is not None else []
        seen: set[str] = set()
        items: list[tuple[str, str]] = []
        for name, url in self._collection_links.get(key, []):
            if url in seen:
                continue
            seen.add(url)
            items.append((name, url))
        return items

    def _materials_for_scraping(self) -> list[dict]:
        """Build name + wear-range rows for exact-wear listing fetch."""
        mode = self.mode_stack.currentIndex()
        if mode == 0:
            template = self._template()
            name = self.name_edit.text().strip()
            if template is None or not name:
                return []
            _appearance, low, high = self._single_wear_and_range()
            return [{"name": name, "min_wear": low, "max_wear": high}]
        if mode == 1:
            materials: list[dict] = []
            seen: set[tuple[str, float, float]] = set()
            for state in self._recipe_material_states:
                name = str(state.get("name") or "").strip()
                selector = state.get("selector")
                if not name or selector is None:
                    continue
                low, high = selector.selected_range()
                key = (normalize_name(name), round(low, 8), round(high, 8))
                if key in seen:
                    continue
                seen.add(key)
                material = state.get("material") if isinstance(state.get("material"), dict) else {}
                materials.append(
                    {
                        "name": name,
                        "min_wear": low,
                        "max_wear": high,
                        "unit_price_cny": material.get("unit_price_cny"),
                        "goods_id": material.get("goods_id"),
                        "youpin_id": material.get("youpin_id"),
                        "buff_id": material.get("buff_id"),
                        "yyyp_id": material.get("yyyp_id"),
                        "c5_id": material.get("c5_id"),
                        "eco_id": material.get("eco_id"),
                    }
                )
            return materials
        if mode == 2:
            return [
                {
                    "name": str(item.get("name") or "").strip(),
                    "min_wear": float(item.get("min_wear") or 0),
                    "max_wear": float(item.get("max_wear") or 1),
                }
                for item in self._special_payload.get("materials", [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
        return []

    def _start_collection(self) -> None:
        if (
            self._collection_running
            or self._collection_timer.isActive()
            or self._collection_queue
            or (
                self._material_worker is not None
                and self._material_worker.isRunning()
            )
        ):
            show_toast(self, "已有采集队列正在运行", style="warning")
            return
        platforms = self._selected_collection_platforms()
        if not platforms:
            show_toast(self, "请先勾选至少一个候选源", style="warning")
            return

        exact_platforms = [key for key in platforms if key in EXACT_WEAR_PROVIDERS]
        link_platforms = [key for key in platforms if key not in EXACT_WEAR_PROVIDERS]
        # Exact-wear platforms scrape via API; when not silent, also open product pages.
        open_platforms = (
            platforms if not self.silent_collection.isChecked() else link_platforms
        )

        materials = self._materials_for_scraping()
        queue: list[tuple[str, str, str]] = []
        for key in open_platforms:
            for name, url in self._collection_items_for_platform(key):
                queue.append((key, name, url))

        can_scrape = bool(exact_platforms and materials)
        if exact_platforms and not materials:
            show_toast(
                self,
                "当前没有可抓取的材料磨损区间，请先读取配方或选择饰品",
                style="warning",
            )
            return
        if not can_scrape and not queue:
            show_toast(self, "当前材料在已勾选候选源中没有可用链接", style="warning")
            return

        self._collection_running = True
        self._collection_stopping = False
        self._collection_started_at = time.monotonic()
        self._set_collection_status_state("running")
        self._pending_alchemy_import = []
        self._collected_items = []
        self.collection_import_button.hide()
        self.collection_save_json_button.hide()
        self._last_scrape_message = ""
        self._eco_retry_materials = []
        self._c5_retry_materials = []
        self._eco_retry_base_items = []
        self._allow_platform_retry_prompt = True
        self._pending_retry_provider = ""
        self._collection_scrape_pending = can_scrape
        self._collection_queue = queue
        self.collection_toggle_button.setText("停止采集")
        names = "、".join(
            next(item.name for item in MARKETPLACES if item.key == key)
            for key in platforms
        )
        if can_scrape:
            self.collection_status.setText(
                f"正在抓取精确磨损挂单 · 候选源 {names}"
            )
            worker = MaterialCollectionWorker(
                materials=materials,
                providers=exact_platforms,
                provider_intervals={
                    key: self._collection_intervals[key].value()
                    for key in exact_platforms
                },
                silent=self.silent_collection.isChecked(),
                parent=self,
            )
            worker.progress.connect(self._set_collection_status_text)
            worker.completed.connect(self._material_collection_scraped)
            worker.finished.connect(worker.deleteLater)
            self._material_worker = worker
            worker.start()
            if queue:
                self._collection_platform = queue[0][0]
                self._process_next_collection_link()
            return

        self._collection_platform = queue[0][0] if queue else ""
        self.collection_status.setText(
            f"准备处理 {len(queue)} 个链接 · 候选源 {names}"
        )
        if queue:
            self._process_next_collection_link()
        else:
            self._finish_collection()

    def _material_collection_scraped(
        self,
        candidates: object,
        message: str,
        retry_meta: object = None,
    ) -> None:
        self._material_worker = None
        rows = [item for item in candidates if isinstance(item, dict)] if isinstance(
            candidates, list
        ) else []
        base = [
            item
            for item in getattr(self, "_eco_retry_base_items", [])
            if isinstance(item, dict)
        ]
        self._eco_retry_base_items = []
        if base:
            rows = dedupe_candidates_keep_cheapest([*base, *rows])
        if self._collection_stopping:
            self._collection_stopping = False
            self._collection_running = False
            self._collection_scrape_pending = False
            self._pending_alchemy_import = []
            self._eco_retry_materials = []
            self._c5_retry_materials = []
            self._pending_retry_provider = ""
            self._allow_platform_retry_prompt = False
            self._last_scrape_message = ""
            started_at = self._collection_started_at
            self._collection_started_at = None
            elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
            self.collection_toggle_button.setEnabled(True)
            self.collection_toggle_button.setText("开始采集")
            self._apply_stopped_collection_results(
                rows,
                message=str(message or "").strip(),
                elapsed=elapsed,
            )
            return
        if not self._collection_running:
            return
        self._collection_scrape_pending = False
        self._pending_alchemy_import = rows
        self._last_scrape_message = str(message or "").strip()
        eco_retry: list[dict] = []
        c5_retry: list[dict] = []
        if isinstance(retry_meta, dict):
            eco_raw = retry_meta.get("eco") or []
            c5_raw = retry_meta.get("c5") or []
            if isinstance(eco_raw, list):
                eco_retry = [dict(item) for item in eco_raw if isinstance(item, dict)]
            if isinstance(c5_raw, list):
                c5_retry = [dict(item) for item in c5_raw if isinstance(item, dict)]
        elif isinstance(retry_meta, list):
            # Backward compatible: old workers emitted ECO-only list.
            eco_retry = [dict(item) for item in retry_meta if isinstance(item, dict)]
        # After a targeted retry, don't re-queue the same provider from meta
        # (worker may still report leftovers); keep other platform's pending list.
        finished_retry = str(getattr(self, "_pending_retry_provider", "") or "")
        self._pending_retry_provider = ""
        if finished_retry == "eco":
            self._eco_retry_materials = eco_retry
        elif finished_retry == "c5":
            self._c5_retry_materials = c5_retry
        else:
            self._eco_retry_materials = eco_retry
            self._c5_retry_materials = c5_retry
        detail = f"已抓取 {len(rows)} 条挂单"
        if self._last_scrape_message:
            detail += f"；{self._last_scrape_message}"
        self.collection_status.setText(detail)
        if not self._collection_queue and not self._collection_timer.isActive():
            self._finish_collection()

    def _process_next_collection_link(self) -> None:
        if not self._collection_queue:
            if not self._collection_scrape_pending:
                self._finish_collection()
            return
        key, name, url = self._collection_queue.pop(0)
        self._collection_platform = key
        silent = self.silent_collection.isChecked()
        if not silent:
            QDesktopServices.openUrl(QUrl(url))
        self._append_collection_history(
            platform=key,
            name=name,
            url=url,
            silent=silent,
        )
        remaining = len(self._collection_queue)
        market = next(item for item in MARKETPLACES if item.key == key)
        scrape_hint = (
            " · 挂单抓取中" if self._collection_scrape_pending else ""
        )
        self.collection_status.setText(
            f"{market.name} · 已处理 {name} · 剩余 {remaining} 个"
            + (" · 静默记录" if silent else " · 已打开商品页")
            + scrape_hint
        )
        if remaining:
            self._collection_timer.start(
                self._collection_intervals[key].value() * 1000
            )
        elif not self._collection_scrape_pending:
            self._finish_collection()

    def _finish_collection(self) -> None:
        self._collection_timer.stop()
        self._collection_queue = []
        items = [
            item for item in self._pending_alchemy_import if isinstance(item, dict)
        ]
        scrape_message = str(getattr(self, "_last_scrape_message", "") or "").strip()
        selected = set(self._selected_collection_platforms())
        allow_prompt = bool(getattr(self, "_allow_platform_retry_prompt", False))
        c5_retry = [
            dict(item)
            for item in getattr(self, "_c5_retry_materials", [])
            if isinstance(item, dict)
        ]
        eco_retry = [
            dict(item)
            for item in getattr(self, "_eco_retry_materials", [])
            if isinstance(item, dict)
        ]

        if (
            c5_retry
            and allow_prompt
            and not self._collection_stopping
            and "c5" in selected
        ):
            # C5 failures stop the platform for this run; no post-run retry prompt.
            self._c5_retry_materials = []

        if (
            eco_retry
            and allow_prompt
            and not self._collection_stopping
            and "eco" in selected
        ):
            # ECO failures stop the platform for this run; no post-run retry prompt.
            self._eco_retry_materials = []

        self._allow_platform_retry_prompt = False
        self._collection_running = False
        self._collection_stopping = False
        self._collection_scrape_pending = False
        self.collection_toggle_button.setText("开始采集")
        self._collection_platform = ""
        started_at = self._collection_started_at
        self._collection_started_at = None
        self._pending_alchemy_import = []
        self._last_scrape_message = ""
        self._eco_retry_materials = []
        self._c5_retry_materials = []
        self._eco_retry_base_items = []
        self._pending_retry_provider = ""
        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        elapsed_text = f"，共计 {elapsed:.1f} 秒"
        counts_text = format_collection_platform_counts(items)
        status = f"采集完成 · {counts_text}{elapsed_text}"
        if scrape_message:
            status += f"；{scrape_message}"
        if items:
            self._collected_items = [dict(item) for item in items]
            self._set_collection_status_text(status, state="complete")
            self.collection_import_button.show()
            self.collection_save_json_button.show()
            if scrape_message:
                # Partial success used to hide per-platform failures (e.g. C5 风控).
                show_toast(self, scrape_message, style="warning")
            return
        self._set_collection_status_text(status, state="complete")
        if any(
            key in EXACT_WEAR_PROVIDERS for key in self._selected_collection_platforms()
        ):
            show_toast(
                self,
                scrape_message or "未抓到可导入炼金计算的精确磨损挂单",
                style="warning",
            )

    def mark_collection_imported(self) -> None:
        """Keep the per-platform breakdown visible after alchemy import."""
        self._set_collection_status_text(
            f"采集完成 · {format_collection_platform_counts(self._collected_items)}"
            " · 已导入炼金计算",
            state="complete",
        )

    def _import_collected_items_to_alchemy(self) -> None:
        if not self._collected_items:
            show_toast(self, "当前没有可导入的采集数据", style="warning")
            return
        self.import_to_alchemy_requested.emit(
            [dict(item) for item in self._collected_items],
            "replace",
        )

    def _save_collected_items_as_json(self) -> None:
        if not self._collected_items:
            show_toast(self, "当前没有可保存的采集数据", style="warning")
            return
        default_name = datetime.now().strftime("采集数据_%Y%m%d_%H%M%S")
        title, accepted = get_wide_text_input(
            self,
            title="保存采集 JSON",
            label="文件名称：",
            value=default_name,
        )
        if not accepted:
            return
        try:
            path = save_collected_json(self._collected_items, title)
        except (OSError, ValueError) as exc:
            show_toast(self, f"保存失败：{exc}", style="warning")
            return
        show_toast(self, f"已保存 {path.name}，可在配方管理中查看", style="success")

    def _apply_stopped_collection_results(
        self,
        items: list[dict],
        *,
        message: str = "",
        elapsed: float = 0.0,
    ) -> None:
        """Keep partial scrape results after the user stops collection."""
        kept = [dict(item) for item in items if isinstance(item, dict)]
        self._collected_items = kept
        counts_text = format_collection_platform_counts(kept)
        elapsed_text = f"，共计 {elapsed:.1f} 秒" if elapsed > 0 else ""
        status = f"已停止采集 · {counts_text}{elapsed_text}"
        detail = str(message or "").strip()
        if detail.startswith("已停止"):
            detail = detail[len("已停止") :].lstrip("；;")
        if detail:
            status += f"；{detail}"
        self._set_collection_status_text(status, state="complete")
        if kept:
            self.collection_import_button.show()
            self.collection_save_json_button.show()
        else:
            self.collection_import_button.hide()
            self.collection_save_json_button.hide()

    def _stop_collection(self) -> None:
        self._collection_timer.stop()
        self._collection_queue = []
        worker = self._material_worker
        if worker is not None and worker.isRunning():
            self._collection_stopping = True
            worker.request_stop()
            self.collection_toggle_button.setEnabled(False)
            self.collection_toggle_button.setText("正在停止…")
            self._set_collection_status_text("正在停止采集…")
        else:
            kept = [
                item
                for item in self._pending_alchemy_import
                if isinstance(item, dict)
            ]
            started_at = self._collection_started_at
            elapsed = (
                max(0.0, time.monotonic() - started_at) if started_at else 0.0
            )
            self._collection_stopping = False
            self._collection_running = False
            self._collection_scrape_pending = False
            self._collection_started_at = None
            self._pending_alchemy_import = []
            self._eco_retry_materials = []
            self._c5_retry_materials = []
            self._eco_retry_base_items = []
            self._allow_platform_retry_prompt = False
            self._pending_retry_provider = ""
            self._last_scrape_message = ""
            self.collection_toggle_button.setText("开始采集")
            self._apply_stopped_collection_results(kept, elapsed=elapsed)
        self._collection_platform = ""

    def _stop_special_collection(self) -> None:
        worker = self._special_worker
        if worker is None or not worker.isRunning():
            return
        self._special_stopping = True
        worker.request_stop()
        self.collection_toggle_button.setEnabled(False)
        self.collection_toggle_button.setText("正在停止…")
        self._set_collection_status_text("正在停止特殊磨损采集…")

    def _set_collection_status_state(self, state: str) -> None:
        self.collection_status.setProperty("collectionState", state)
        self.collection_status.style().unpolish(self.collection_status)
        self.collection_status.style().polish(self.collection_status)

    def _set_collection_status_text(self, text: str, *, state: str = "running") -> None:
        self._set_collection_status_state(state)
        self.collection_status.setText(str(text or ""))

    def _save_collection_settings(self, *_args) -> None:
        update_app_settings(
            updates={
                "material_collection_intervals": {
                    key: spin.value()
                    for key, spin in self._collection_intervals.items()
                },
                "material_collection_silent": self.silent_collection.isChecked()
                if hasattr(self, "silent_collection")
                else self._saved_silent,
                "material_collection_include_alternatives": (
                    self.include_alternatives.isChecked()
                    if hasattr(self, "include_alternatives")
                    else self._saved_include_alternatives
                ),
                "special_candidate_sources": {
                    key: checkbox.isChecked()
                    for key, checkbox in self._source_checks.items()
                },
            }
        )

    @staticmethod
    def _append_collection_history(
        *,
        platform: str,
        name: str,
        url: str,
        silent: bool,
    ) -> None:
        import time

        payload = read_json_dict(MATERIAL_COLLECTION_HISTORY_FILE)
        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        items.append(
            {
                "platform": platform,
                "name": name,
                "url": url,
                "silent": bool(silent),
                "processed_at": time.time(),
            }
        )
        write_json(
            MATERIAL_COLLECTION_HISTORY_FILE,
            {"items": items[-1000:]},
            ensure_parent=True,
        )
