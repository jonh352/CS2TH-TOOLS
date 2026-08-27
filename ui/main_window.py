"""Top-level CS2TH branded application shell."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config import (
    APP_ICON,
    APP_NAME,
    APP_VERSION,
    BRAND_IMAGE,
    CLOSE_BEHAVIOR_MINIMIZE,
)
from core.app_protocol import FOCUS_COMMAND, recipe_reference_from_command
from core.auth_client import AuthClient, AuthSession, has_tradeup_access
from core.client_update import is_version_older
from core.close_behavior_prefs import load_close_behavior
from ui.access_lock import apply_page_interaction_lock
from ui.dialogs.account_dialog import AccountDialog
from ui.dialogs.alert_dialog import ClientUpdateDialog
from ui.dialogs.information_dialogs import SettingsDialog
from ui.login_dialog import LoginDialog
from ui.theme import apply_theme
from ui.widgets.toast import ToastWidget
from ui.workers.auth import LogoutWorker, SessionValidationWorker


PAGE_DEFINITIONS = (
    ("inventory", "Steam 库存"),
    ("alchemy", "炼金计算"),
    ("collection_presets", "采集预设"),
    ("recipes", "配方管理"),
    ("simulation", "炼金模拟"),
    ("special", "特殊磨损"),
    ("platforms", "材料采集"),
    ("about", "关于"),
)

ACCOUNT_PLAN_LABELS = {
    "tradeup": "汰换会员",
    "terminal": "终端会员",
    "selection": "选品会员",
    "all_access": "大会员",
}


@dataclass(frozen=True)
class _NavigationRoute:
    page_key: str
    subroute: str = ""
    label: str = field(default="", compare=False)


class _BrandHomeLink(QFrame):
    """Clickable brand block linking back to the CS2TH website."""

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl("https://cs2th.cn"))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("CS2TH", "CS2TH Tools")
        self.auth_client = AuthClient()
        self.auth_session: AuthSession | None = self.auth_client.load_local_session()
        self._auth_validation_worker: SessionValidationWorker | None = None
        self._logout_worker: LogoutWorker | None = None
        self._access_allowed = False
        self._pending_recipe_reference = ""
        self._navigation_history: list[_NavigationRoute] = []
        self._navigation_history_index = -1
        self._restoring_navigation = False
        self._auth_recheck_timer = QTimer(self)
        self._auth_recheck_timer.setSingleShot(True)
        self._auth_recheck_timer.timeout.connect(self._start_auth_validation)
        self.theme_name = str(self.settings.value("theme", "dark"))
        if self.theme_name not in ("dark", "light"):
            self.theme_name = "dark"
        apply_theme(QApplication.instance(), self.theme_name)

        self.setWindowTitle(f"{APP_NAME} · v{APP_VERSION}")
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1150, 860)
        self.setMinimumSize(960, 680)

        root_widget = QWidget()
        root_widget.setObjectName("appRoot")
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        self._access_banner = QFrame()
        self._access_banner.setObjectName("accessLockBanner")
        self._access_banner.hide()
        banner_row = QHBoxLayout(self._access_banner)
        banner_row.setContentsMargins(16, 8, 16, 8)
        banner_row.setSpacing(12)
        self._access_banner_label = QLabel()
        self._access_banner_label.setObjectName("accessLockBannerLabel")
        self._access_banner_label.setWordWrap(True)
        banner_row.addWidget(self._access_banner_label, 1)
        self._access_banner_action = QPushButton("去登录")
        self._access_banner_action.setObjectName("accessLockBannerBtn")
        self._access_banner_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self._access_banner_action.clicked.connect(self._account_clicked)
        banner_row.addWidget(self._access_banner_action, 0)
        root.addWidget(self._access_banner)

        self._client_update_prompt_shown = False

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self._active_page_key = ""
        self._startup_placeholder = QLabel("正在加载…")
        self._startup_placeholder.setObjectName("muted")
        self._startup_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self._startup_placeholder)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(root_widget)
        self._build_navigation_history_overlay(root_widget)
        self.stack.installEventFilter(self)
        self.toast = ToastWidget(root_widget, top_inset_px=70)
        self.toast.hide()

        # Browse-first: pages stay navigable; features stay locked until access is granted.
        self._set_access_ui(
            allowed=False,
            message=(
                "正在验证汰换小助手使用权限…"
                if self.auth_session is not None
                else "登录并具备汰换会员权益后可使用功能。"
            ),
            show_login=self.auth_session is None,
        )
        QTimer.singleShot(0, self._start_auth_validation)
        QTimer.singleShot(0, lambda: self._activate("alchemy"))
        QTimer.singleShot(0, self._position_navigation_history_overlay)
        self._sync_account_button()

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("topBar")
        topbar.setFixedHeight(70)
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(24, 10, 20, 10)
        layout.setSpacing(6)

        brand_link = _BrandHomeLink()
        brand_link.setObjectName("brandHomeLink")
        brand_link.setCursor(Qt.CursorShape.PointingHandCursor)
        brand_link.setToolTip("打开 CS2TH 官网")
        brand_link_layout = QHBoxLayout(brand_link)
        brand_link_layout.setContentsMargins(0, 0, 0, 0)
        brand_link_layout.setSpacing(6)

        logo = QLabel()
        logo.setObjectName("brandLogo")
        logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if BRAND_IMAGE.is_file():
            pixmap = QPixmap(str(BRAND_IMAGE))
            logo.setPixmap(
                pixmap.scaled(
                    40,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_link_layout.addWidget(logo)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel("CS2TH")
        name.setObjectName("brandName")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        sub = QLabel(f"汰换小助手 · v{APP_VERSION}")
        sub.setObjectName("brandSub")
        sub.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        brand.addWidget(name)
        brand.addWidget(sub)
        brand_link_layout.addLayout(brand)
        layout.addWidget(brand_link)
        layout.addSpacing(10)
        divider = QFrame()
        divider.setObjectName("brandDivider")
        divider.setFixedSize(1, 32)
        layout.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(8)

        self.nav_buttons: dict[str, QPushButton] = {}
        for key, text in PAGE_DEFINITIONS:
            button = QPushButton(text)
            button.setObjectName("navButton")
            button.setProperty("active", False)
            button.setMinimumWidth(72)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, page=key: self._activate(page))
            layout.addWidget(button)
            self.nav_buttons[key] = button
        layout.addStretch(1)

        self.account_button = QPushButton("登录")
        self.account_button.setObjectName("accountButton")
        self.account_button.setMaximumWidth(160)
        self.account_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_button.clicked.connect(self._account_clicked)
        layout.addWidget(self.account_button)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setToolTip("切换明暗主题")
        self.theme_button.setFixedSize(38, 38)
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_button)
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setToolTip("设置")
        self.settings_button.setFixedSize(38, 38)
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self._open_settings)
        layout.addWidget(self.settings_button)
        self._sync_theme_button()
        return topbar

    def _build_navigation_history_overlay(self, parent: QWidget) -> None:
        self._navigation_history_overlay = QFrame(parent)
        self._navigation_history_overlay.setObjectName("navigationHistoryOverlay")
        self._navigation_history_overlay.setFixedSize(60, 20)
        row = QHBoxLayout(self._navigation_history_overlay)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.navigation_back_button = QPushButton("←", self._navigation_history_overlay)
        self.navigation_back_button.setObjectName("navigationHistoryButton")
        self.navigation_back_button.setFixedSize(28, 20)
        self.navigation_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.navigation_back_button.setToolTip("无可返回界面")
        self.navigation_back_button.setAccessibleName("返回上一个界面")
        self.navigation_back_button.setEnabled(False)
        self.navigation_back_button.clicked.connect(self._navigate_back)
        row.addWidget(self.navigation_back_button)
        self.navigation_forward_button = QPushButton(
            "→", self._navigation_history_overlay
        )
        self.navigation_forward_button.setObjectName("navigationHistoryButton")
        self.navigation_forward_button.setFixedSize(28, 20)
        self.navigation_forward_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.navigation_forward_button.setToolTip("无可前进界面")
        self.navigation_forward_button.setAccessibleName("前进到下一个界面")
        self.navigation_forward_button.setEnabled(False)
        self.navigation_forward_button.clicked.connect(self._navigate_forward)
        row.addWidget(self.navigation_forward_button)
        self._navigation_history_overlay.raise_()

    def _position_navigation_history_overlay(self) -> None:
        if not hasattr(self, "_navigation_history_overlay"):
            return
        self._navigation_history_overlay.move(12, self.stack.y())
        self._navigation_history_overlay.raise_()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is getattr(self, "stack", None) and event.type() in {
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.Show,
        }:
            QTimer.singleShot(0, self._position_navigation_history_overlay)
        return super().eventFilter(watched, event)

    def _build_page(self, key: str) -> QWidget:
        if key == "alchemy":
            from ui.pages.alchemy import AlchemyPage

            return AlchemyPage(self.stack)
        if key == "collection_presets":
            from ui.pages.collection_presets import CollectionPresetPage

            page = CollectionPresetPage(self.stack)
            page.import_to_collection_requested.connect(
                self._on_collection_preset_import
            )
            return page
        if key == "simulation":
            from ui.pages.alchemy_simulation import AlchemySimulationPage

            return AlchemySimulationPage(self.stack)
        if key == "recipes":
            from ui.pages.recipe_manage import RecipeManagePage

            page = RecipeManagePage(self.stack)
            page.import_to_simulation_requested.connect(
                self._on_recipe_import_to_simulation
            )
            page.import_to_collection_requested.connect(
                self._on_recipe_import_to_collection
            )
            page.import_json_to_alchemy_requested.connect(
                self._on_saved_json_import_to_alchemy
            )
            return page
        if key == "special":
            from ui.pages.special_wear import SpecialWearPage

            page = SpecialWearPage(self.stack)
            page.materials_requested.connect(self._on_special_wear_materials_requested)
            return page
        if key == "inventory":
            from ui.pages.inventory import InventoryPage

            page = InventoryPage(self.stack)
            page.import_to_alchemy_requested.connect(
                self._on_inventory_import_to_alchemy
            )
            page.import_to_simulation_requested.connect(
                self._on_inventory_import_to_simulation
            )
            return page
        if key == "platforms":
            from ui.pages.platforms import PlatformPage

            page = PlatformPage(self.stack)
            page.import_to_simulation_requested.connect(
                self._on_recipe_import_to_simulation
            )
            page.import_to_alchemy_requested.connect(
                self._on_collection_import_to_alchemy
            )
            return page
        if key == "about":
            from ui.pages.about import AboutPage

            return AboutPage(self.stack)
        raise KeyError(key)

    def _on_special_wear_materials_requested(self, payload: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        page = self._ensure_page("platforms")
        if hasattr(page, "show_special_wear_materials"):
            page.show_special_wear_materials(payload)
        self._activate("platforms")

    def _ensure_page(self, key: str) -> QWidget:
        page = self.pages.get(key)
        if page is not None:
            return page
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            page = self._build_page(key)
            page.setObjectName("contentArea")
            page.setProperty("cs2thPaletteTheme", self.theme_name)
            self.pages[key] = page
            self.stack.addWidget(page)
            route_signal = getattr(page, "navigation_route_changed", None)
            if route_signal is not None:
                route_signal.connect(
                    lambda _subroute="", page_key=key: self._on_page_route_changed(
                        page_key
                    )
                )
            if key != "about":
                apply_page_interaction_lock(page, not self._access_allowed)
            return page
        finally:
            QApplication.restoreOverrideCursor()

    def _activate(self, key: str, *, record_history: bool = True) -> None:
        previous_key = self._active_page_key
        previous = self.pages.get(self._active_page_key)
        if (
            self._active_page_key == "recipes"
            and previous is not None
            and hasattr(previous, "prepare_leave_for_sidebar_switch")
        ):
            previous.prepare_leave_for_sidebar_switch()
        page = self._ensure_page(key)
        self.stack.setCurrentWidget(page)
        if page.property("cs2thPaletteTheme") != self.theme_name:
            self._repolish_tree(page)
            page.setProperty("cs2thPaletteTheme", self.theme_name)
        if self._startup_placeholder is not None:
            self.stack.removeWidget(self._startup_placeholder)
            self._startup_placeholder.deleteLater()
            self._startup_placeholder = None
        self._active_page_key = key
        if key == "recipes" and hasattr(page, "refresh_from_disk"):
            page.refresh_from_disk()
        if key != "about":
            apply_page_interaction_lock(page, not self._access_allowed)

        for button_key in {previous_key, key}:
            button = self.nav_buttons.get(button_key)
            if button is None:
                continue
            button.setProperty("active", button_key == key)
            button.style().unpolish(button)
            button.style().polish(button)
        if record_history and not self._restoring_navigation:
            self._record_current_navigation_route()

    def _current_navigation_route(self) -> _NavigationRoute | None:
        key = self._active_page_key
        if not key:
            return None
        page = self.pages.get(key)
        subroute = ""
        label = dict(PAGE_DEFINITIONS).get(key, key)
        if page is not None:
            route_getter = getattr(page, "navigation_subroute", None)
            if callable(route_getter):
                subroute = str(route_getter() or "")
            label_getter = getattr(page, "navigation_route_label", None)
            if callable(label_getter):
                page_label = str(label_getter() or "").strip()
                if page_label:
                    label = page_label
        return _NavigationRoute(key, subroute, label)

    def _record_current_navigation_route(self) -> None:
        route = self._current_navigation_route()
        if route is None:
            return
        if (
            0 <= self._navigation_history_index < len(self._navigation_history)
            and self._navigation_history[self._navigation_history_index] == route
        ):
            self._sync_navigation_history_buttons()
            return
        if self._navigation_history_index + 1 < len(self._navigation_history):
            del self._navigation_history[self._navigation_history_index + 1 :]
        self._navigation_history.append(route)
        if len(self._navigation_history) > 50:
            overflow = len(self._navigation_history) - 50
            del self._navigation_history[:overflow]
        self._navigation_history_index = len(self._navigation_history) - 1
        self._sync_navigation_history_buttons()

    def _on_page_route_changed(self, page_key: str) -> None:
        if self._restoring_navigation or page_key != self._active_page_key:
            return
        self._record_current_navigation_route()

    def _navigate_back(self) -> None:
        if self._navigation_history_index <= 0:
            return
        self._navigation_history_index -= 1
        self._restore_navigation_route(
            self._navigation_history[self._navigation_history_index]
        )

    def _navigate_forward(self) -> None:
        if self._navigation_history_index + 1 >= len(self._navigation_history):
            return
        self._navigation_history_index += 1
        self._restore_navigation_route(
            self._navigation_history[self._navigation_history_index]
        )

    def _restore_navigation_route(self, route: _NavigationRoute) -> None:
        self._restoring_navigation = True
        try:
            if route.page_key != self._active_page_key:
                self._activate(route.page_key, record_history=False)
            page = self.pages.get(route.page_key)
            restore = getattr(page, "restore_navigation_subroute", None)
            if callable(restore):
                restore(route.subroute)
        finally:
            self._restoring_navigation = False
            self._sync_navigation_history_buttons()

    def _sync_navigation_history_buttons(self) -> None:
        if not hasattr(self, "navigation_back_button"):
            return
        can_back = self._navigation_history_index > 0
        can_forward = (
            self._navigation_history_index + 1 < len(self._navigation_history)
        )
        self.navigation_back_button.setEnabled(can_back)
        self.navigation_forward_button.setEnabled(can_forward)
        self.navigation_back_button.setToolTip(
            f"返回：{self._navigation_history[self._navigation_history_index - 1].label}"
            if can_back
            else "无可返回界面"
        )
        self.navigation_forward_button.setToolTip(
            f"前进：{self._navigation_history[self._navigation_history_index + 1].label}"
            if can_forward
            else "无可前进界面"
        )

    def _warn_feature_locked(self) -> None:
        if self._access_allowed:
            return
        if self.auth_session is None:
            message = "请先登录 CS2TH 账号后再使用功能"
        else:
            message = "当前账号无汰换会员权益，暂不可使用功能"
        self.toast.show_toast(message, style="warning")

    def _on_recipe_import_to_simulation(self, recipe: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(recipe, dict):
            return
        simulation = self._ensure_page("simulation")
        error = simulation.import_substrates_from_recipe_dict(recipe)
        if error:
            self.toast.show_toast(str(error), style="warning")
            return
        self.toast.show_toast("已导入配方底物", style="success")
        self._activate("simulation")

    def _on_recipe_import_to_collection(self, recipe: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(recipe, dict):
            return
        title = str(recipe.get("title") or "").strip()
        inner = recipe.get("recipe")
        if isinstance(inner, dict):
            recipe = inner
        collection = self._ensure_page("platforms")
        error = collection.show_saved_recipe_materials(recipe, title=title)
        if error:
            self.toast.show_toast(str(error), style="warning")
            return
        self.toast.show_toast("配方材料已导入采集页", style="success")
        self._activate("platforms")

    def handle_external_command(self, command: str) -> None:
        """Restore the window and handle a command forwarded by another launch."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        if command == FOCUS_COMMAND:
            return
        try:
            reference = recipe_reference_from_command(command)
        except ValueError as exc:
            self.toast.show_toast(str(exc), style="warning")
            return
        self._pending_recipe_reference = reference
        self._apply_pending_recipe_import()

    def _apply_pending_recipe_import(self) -> None:
        reference = self._pending_recipe_reference
        if not reference:
            return
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        self._pending_recipe_reference = ""
        self._activate("platforms")
        collection = self._ensure_page("platforms")
        if hasattr(collection, "load_recipe_reference"):
            collection.load_recipe_reference(reference)

    def _on_collection_preset_import(self, payload: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(payload, dict):
            return
        items = payload.get("items")
        title = str(payload.get("title") or "").strip()
        collection = self._ensure_page("platforms")
        error = collection.show_collection_preset_materials(items, title=title)
        if error:
            self.toast.show_toast(str(error), style="warning")
            return
        self.toast.show_toast("采集预设已导入采集页", style="success")
        self._activate("platforms")

    def _on_inventory_import_to_alchemy(
        self, items: object, mode: object = None
    ) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(items, list) or not items:
            return
        alchemy = self._ensure_page("alchemy")
        if mode == "replace":
            alchemy.apply_inventory_import_replace(items)
        else:
            alchemy.apply_inventory_import_merge(items)
        inventory = self.pages.get("inventory")
        if inventory is not None:
            inventory.clear_selected_after_successful_import()
        self.toast.show_toast("库存底物已导入计算页", style="success")
        self._activate("alchemy")

    def _on_collection_import_to_alchemy(
        self, items: object, mode: object = None
    ) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(items, list) or not items:
            return
        alchemy = self._ensure_page("alchemy")
        if mode == "replace":
            alchemy.apply_inventory_import_replace(items, source_label="采集")
        else:
            alchemy.apply_inventory_import_merge(items)
        count = len(items)
        self.toast.show_toast(
            f"已将采集挂单导入炼金计算（{count} 条）",
            style="success",
        )
        self._activate("alchemy")
        platforms = self.pages.get("platforms")
        if platforms is not None and hasattr(platforms, "mark_collection_imported"):
            platforms.mark_collection_imported()

    def _on_saved_json_import_to_alchemy(self, items: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(items, list) or not items:
            return
        alchemy = self._ensure_page("alchemy")
        alchemy.apply_inventory_import_replace(items, source_label="采集 JSON")
        self.toast.show_toast(
            f"已载入采集 JSON（{len(items)} 条），并替换原底物",
            style="success",
        )
        self._activate("alchemy")

    @staticmethod
    def _inventory_item_quality_key(item: dict) -> str:
        for key in ("market_name", "market_hash_name", "name"):
            if "★" in str(item.get(key) or ""):
                return "contraband"
        rarity = str(item.get("rarity") or "").strip().lower()
        if rarity.startswith("rarity_"):
            rarity = rarity.split("_", 1)[1]
        if rarity.endswith("_weapon"):
            rarity = rarity[: -len("_weapon")].strip("_")
        return rarity or "common"

    def _on_inventory_import_to_simulation(self, items: object) -> None:
        if not self._access_allowed:
            self._warn_feature_locked()
            return
        if not isinstance(items, list) or not items:
            return
        simulation = self._ensure_page("simulation")
        slot_count = 5 if self._inventory_item_quality_key(items[0]) == "ancient" else 10
        current = simulation.filled_substrate_count_for_slot_count(slot_count)
        if current + len(items) > slot_count:
            self.toast.show_toast(
                f"底物数量过多（当前共 {current + len(items)} 件）",
                style="warning",
            )
            return
        error = simulation.import_inventory_items(items, slot_count=slot_count)
        if error:
            self.toast.show_toast(str(error), style="warning")
            return
        inventory = self.pages.get("inventory")
        if inventory is not None:
            inventory.clear_selected_after_successful_import()
        self.toast.show_toast("库存底物已导入模拟页", style="success")
        self._activate("simulation")

    def _toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.settings.setValue("theme", self.theme_name)
        apply_theme(QApplication.instance(), self.theme_name)
        self._sync_theme_button()
        # Qt caches palette brushes referenced by QSS. Re-polish only what is
        # currently on screen; hidden lazy pages refresh when activated.
        self._repolish_tree(self)
        current = self.pages.get(self._active_page_key)
        if current is not None:
            current.setProperty("cs2thPaletteTheme", self.theme_name)

    @staticmethod
    def _repolish_tree(root: QWidget) -> None:
        for widget in (root, *root.findChildren(QWidget)):
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()

    def _sync_theme_button(self) -> None:
        if not hasattr(self, "theme_button"):
            return
        if self.theme_name == "dark":
            self.theme_button.setText("☀")
            self.theme_button.setToolTip("切换到亮色主题")
        else:
            self.theme_button.setText("☾")
            self.theme_button.setToolTip("切换到深色主题")

    def _open_settings(self) -> None:
        SettingsDialog(self).exec()

    def _sync_account_button(self) -> None:
        if self.auth_session:
            username = str(self.auth_session.account.username or "").strip() or "已登录"
            self.account_button.setText(username)
            lines = self._account_entitlement_lines()
            detail = "\n".join(lines) if lines else "当前没有有效会员权益"
            self.account_button.setToolTip(f"{detail}\n\n点击查看账号 / 退出登录")
        else:
            self.account_button.setText("登录 CS2TH")
            self.account_button.setToolTip("使用 cs2th.cn 账号登录")

    def _account_entitlement_lines(self) -> list[str]:
        if not self.auth_session:
            return []
        now = time.time()
        subscriptions = self.auth_session.account.subscriptions or {}
        active = [
            (key, float(until or 0))
            for key, until in subscriptions.items()
            if float(until or 0) > now
        ]
        active.sort(key=lambda item: (item[0] != "all_access", item[0]))
        return [
            f"{ACCOUNT_PLAN_LABELS.get(key, key)} 到期 "
            f"{datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M')}"
            for key, until in active
        ]

    def _set_access_ui(
        self,
        *,
        allowed: bool,
        message: str,
        show_login: bool,
    ) -> None:
        self._access_allowed = allowed
        for button in self.nav_buttons.values():
            button.setEnabled(True)
        for key, page in self.pages.items():
            if key == "about":
                continue
            apply_page_interaction_lock(page, not allowed)
        if allowed:
            self._access_banner.hide()
            QTimer.singleShot(0, self._position_navigation_history_overlay)
            return
        self._access_banner_label.setText(message)
        self._access_banner_action.setText("去登录" if show_login else "查看账号")
        self._access_banner_action.show()
        self._access_banner.show()
        QTimer.singleShot(0, self._position_navigation_history_overlay)

    def _schedule_auth_recheck(self, session: AuthSession) -> None:
        delay_seconds = 300.0
        beta = session.public_beta or {}
        if not beta.get("tradeup") and session.account.member_until > 0:
            delay_seconds = min(
                delay_seconds,
                max(1.0, session.account.member_until - time.time()),
            )
        self._auth_recheck_timer.start(max(1_000, int(delay_seconds * 1_000)))

    def _apply_access_session(
        self, session: AuthSession | None, *, error: str = ""
    ) -> None:
        self.auth_session = session
        self._sync_account_button()
        self._maybe_show_client_update(session)
        allowed = bool(has_tradeup_access(session) and not error)
        if allowed and session is not None:
            self._set_access_ui(allowed=True, message="", show_login=False)
            self._schedule_auth_recheck(session)
            if self._pending_recipe_reference:
                self._apply_pending_recipe_import()
            elif not self._active_page_key:
                self._activate("alchemy")
            return
        self._auth_recheck_timer.stop()
        if session is not None:
            self._auth_recheck_timer.start(30_000 if error else 300_000)
        if error:
            message = (
                f"暂时无法验证使用权限：{error}。功能暂不可用。"
            )
            show_login = session is None
        elif session is None:
            message = (
                "登录并具备汰换会员权益后可使用所有功能。"
            )
            show_login = True
        else:
            message = (
                "汰换公测已关闭；当前账号需要汰换会员或大会员权益。"
                "可浏览页面，暂不可使用功能。"
            )
            show_login = False
        self._set_access_ui(
            allowed=False,
            message=message,
            show_login=show_login,
        )
        if not self._active_page_key:
            self._activate("alchemy")

    def _maybe_show_client_update(self, session: AuthSession | None) -> None:
        if self._client_update_prompt_shown:
            return
        info = session.client_update if session is not None else None
        if info is None or not is_version_older(APP_VERSION, info.latest_version):
            return
        self._client_update_prompt_shown = True
        ClientUpdateDialog(
            info.latest_version,
            APP_VERSION,
            info.download_url,
            self,
        ).exec()

    def _start_auth_validation(self) -> None:
        if self.auth_session is None or not self.auth_client.enabled:
            self._apply_access_session(
                self.auth_session,
                error=(
                    "账号服务未启用"
                    if not self.auth_client.enabled
                    else ""
                ),
            )
            return
        if (
                self._auth_validation_worker is not None
                and self._auth_validation_worker.isRunning()
        ):
            return
        self._auth_validation_worker = SessionValidationWorker(
            self.auth_client, self.auth_session, self
        )
        self._auth_validation_worker.completed.connect(
            self._auth_validation_finished
        )
        self._auth_validation_worker.start()

    def _auth_validation_finished(
        self, session: AuthSession | None, error: str
    ) -> None:
        self._apply_access_session(session, error=error)
        if error:
            self.account_button.setToolTip(
                "暂时无法连接 cs2th.cn，功能已锁定直至完成权限验证"
            )
        elif session is None:
            self.toast.show_toast("CS2TH 登录已过期，请重新登录", style="info")

    def _account_clicked(self) -> None:
        if self.auth_session is not None:
            dialog = AccountDialog(
                username=str(self.auth_session.account.username or ""),
                entitlement_lines=self._account_entitlement_lines(),
                parent=self,
            )
            if dialog.exec() != AccountDialog.DialogCode.Accepted:
                return
            session = self.auth_session
            self.auth_client.clear_local_session()
            self._apply_access_session(None)
            self._logout_worker = LogoutWorker(self.auth_client, session, self)
            self._logout_worker.start()
            return
        dialog = LoginDialog(self.auth_client, self)
        dialog.logged_in.connect(self._logged_in)
        dialog.exec()

    def _logged_in(self, session: AuthSession) -> None:
        self._apply_access_session(session)

    def closeEvent(self, event) -> None:
        if load_close_behavior() == CLOSE_BEHAVIOR_MINIMIZE:
            event.ignore()
            self.showMinimized()
            return
        inventory = self.pages.get("inventory")
        if inventory is not None and hasattr(inventory, "close_workers"):
            inventory.close_workers()
        alchemy = self.pages.get("alchemy")
        if alchemy is not None:
            if hasattr(alchemy, "save_step2_wear_prefs_for_exit"):
                alchemy.save_step2_wear_prefs_for_exit()
            if hasattr(alchemy, "_on_step3_stop_calc_requested"):
                alchemy._on_step3_stop_calc_requested()
        simulation = self.pages.get("simulation")
        worker = getattr(simulation, "_fetch_worker", None)
        if worker is not None and hasattr(worker, "isRunning") and worker.isRunning():
            worker.requestInterruption()
            worker.wait(1500)
        for worker in (self._auth_validation_worker, self._logout_worker):
            if worker is not None and worker.isRunning():
                worker.wait(1500)
        super().closeEvent(event)
