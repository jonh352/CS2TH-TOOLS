"""Top-level CS2TH branded application shell."""

from __future__ import annotations

import time

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCursor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
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
from core.auth_client import AuthClient, AuthSession, has_tradeup_access
from core.close_behavior_prefs import load_close_behavior
from ui.login_dialog import LoginDialog
from ui.dialogs.information_dialogs import SettingsDialog
from ui.theme import apply_theme
from ui.widgets.toast import ToastWidget
from ui.workers.auth import LogoutWorker, SessionValidationWorker


PAGE_DEFINITIONS = (
    ("inventory", "Steam 库存"),
    ("alchemy", "炼金计算"),
    ("recipes", "配方管理"),
    ("simulation", "炼金模拟"),
    ("special", "特殊磨损"),
    ("platforms", "材料采集"),
    ("about", "关于"),
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("CS2TH", "CS2TH Tools")
        self.auth_client = AuthClient()
        self.auth_session: AuthSession | None = self.auth_client.load_local_session()
        self._auth_validation_worker: SessionValidationWorker | None = None
        self._logout_worker: LogoutWorker | None = None
        self._access_allowed = False
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

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self._active_page_key = ""
        self._startup_placeholder = QLabel("正在加载炼金计算…")
        self._startup_placeholder.setObjectName("muted")
        self._startup_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self._startup_placeholder)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(root_widget)
        self.toast = ToastWidget(root_widget, top_inset_px=70)
        self.toast.hide()

        # Let the branded shell paint once before constructing the first large
        # feature page. Remaining pages stay lazy for the entire session.
        self._show_access_gate(
            "正在验证汰换小助手使用权限…"
            if self.auth_session is not None
            else "请先登录 CS2TH 账号后使用汰换小助手"
        )
        QTimer.singleShot(0, self._start_auth_validation)
        self._sync_account_button()

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("topBar")
        topbar.setFixedHeight(70)
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(24, 10, 20, 10)
        layout.setSpacing(6)

        logo = QLabel()
        logo.setObjectName("brandLogo")
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
        layout.addWidget(logo)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel("CS2TH")
        name.setObjectName("brandName")
        sub = QLabel(f"汰换小助手 · v{APP_VERSION}")
        sub.setObjectName("brandSub")
        brand.addWidget(name)
        brand.addWidget(sub)
        layout.addLayout(brand)
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
        self.account_button.setMaximumWidth(180)
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

    def _build_page(self, key: str) -> QWidget:
        if key == "alchemy":
            from ui.pages.alchemy import AlchemyPage

            return AlchemyPage(self.stack)
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
            return page
        if key == "about":
            from ui.pages.about import AboutPage

            return AboutPage(self.stack)
        raise KeyError(key)

    def _on_special_wear_materials_requested(self, payload: object) -> None:
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
            return page
        finally:
            QApplication.restoreOverrideCursor()

    def _activate(self, key: str) -> None:
        if key != "about" and not self._access_allowed:
            self.toast.show_toast(
                "请先登录，并确认账号具有汰换会员/大会员权益或处于汰换公测期",
                style="warning",
            )
            return
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

        for button_key in {previous_key, key}:
            button = self.nav_buttons.get(button_key)
            if button is None:
                continue
            button.setProperty("active", button_key == key)
            button.style().unpolish(button)
            button.style().polish(button)

    def _on_recipe_import_to_simulation(self, recipe: object) -> None:
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

    def _on_inventory_import_to_alchemy(
        self, items: object, mode: object = None
    ) -> None:
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
            suffix = " · 会员" if self.auth_session.account.member else ""
            self.account_button.setText(self.auth_session.account.username + suffix)
            self.account_button.setToolTip("点击退出登录")
        else:
            self.account_button.setText("登录 CS2TH")
            self.account_button.setToolTip("使用 cs2th.cn 账号登录")

    def _show_access_gate(self, message: str) -> None:
        if self._startup_placeholder is None:
            self._startup_placeholder = QLabel()
            self._startup_placeholder.setObjectName("muted")
            self._startup_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.stack.addWidget(self._startup_placeholder)
        self._startup_placeholder.setText(message)
        self.stack.setCurrentWidget(self._startup_placeholder)
        self._active_page_key = ""
        for key, button in self.nav_buttons.items():
            button.setEnabled(key == "about")
            button.setProperty("active", False)
            button.style().unpolish(button)
            button.style().polish(button)

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
        allowed = bool(has_tradeup_access(session) and not error)
        self._access_allowed = allowed
        if allowed and session is not None:
            for button in self.nav_buttons.values():
                button.setEnabled(True)
            self._schedule_auth_recheck(session)
            if not self._active_page_key:
                self._activate("alchemy")
            return
        self._auth_recheck_timer.stop()
        if session is not None:
            self._auth_recheck_timer.start(30_000 if error else 300_000)
        if error:
            message = f"暂时无法验证使用权限：{error}"
        elif session is None:
            message = "请先登录 CS2TH 账号后使用汰换小助手"
        else:
            message = "汰换公测已关闭；当前账号需要汰换会员或大会员权益"
        self._show_access_gate(message)

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
            answer = QMessageBox.question(self, "退出登录", "确定退出当前 CS2TH 账号吗？")
            if answer == QMessageBox.StandardButton.Yes:
                session = self.auth_session
                self.auth_client.clear_local_session()
                self._apply_access_session(None)
                self._logout_worker = LogoutWorker(
                    self.auth_client, session, self
                )
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
