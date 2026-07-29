"""炼金计算：选择 Steam 账号并一键导入其本地库存。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.inventory_steam_accounts import (
    combo_display_name_for_profile,
    get_active_profile_id,
    list_profile_entries,
)
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)


class ImportSteamInventoryDialog(QDialog):
    """选择账号后返回 ``(profile_id, mode)``，``mode`` 为 ``replace`` / ``merge``；取消为 ``None``。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("loginDialog")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._result: tuple[str, str] | None = None

        overlay, box, box_layout, close_btn = self._build_shell()
        close_btn.clicked.connect(self.reject)

        hint = QLabel("将导入该账号本地已缓存的全部库存（需先在「Steam 库存」获取过）。")
        hint.setObjectName("loginError")
        hint.setWordWrap(True)
        box_layout.addWidget(hint)

        row = QHBoxLayout()
        row.addWidget(QLabel("Steam 账号"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(220)
        row.addWidget(self.account_combo, 1)
        box_layout.addLayout(row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.merge_btn = QPushButton("追加导入")
        self.merge_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.replace_btn = QPushButton("替换导入")
        self.replace_btn.setObjectName("primaryButton")
        self.replace_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row.addWidget(self.merge_btn)
        btn_row.addWidget(self.replace_btn)
        box_layout.addLayout(btn_row)

        self.merge_btn.clicked.connect(lambda: self._accept_mode("merge"))
        self.replace_btn.clicked.connect(lambda: self._accept_mode("replace"))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(overlay)

        self._reload_accounts()
        install_dialog_topmost_follow_parent(self)
        apply_frameless_modal_geometry(self, parent)

    def _build_shell(self) -> tuple[QWidget, QFrame, QVBoxLayout, QPushButton]:
        overlay = QWidget(self)
        overlay.setObjectName("loginOverlay")
        outer = QVBoxLayout(overlay)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        box = QFrame()
        box.setObjectName("loginBox")
        box.setFixedWidth(440)
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(20, 16, 20, 16)
        box_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("导入 Steam 库存")
        title.setObjectName("loginTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        close_btn = QPushButton("×")
        close_btn.setObjectName("loginCloseBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        title_row.addWidget(close_btn)
        box_layout.addLayout(title_row)

        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(box)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)
        return overlay, box, box_layout, close_btn

    def _reload_accounts(self) -> None:
        self.account_combo.clear()
        active = get_active_profile_id()
        selected = -1
        for i, entry in enumerate(list_profile_entries()):
            pid = str(entry.get("id") or "").strip()
            if not pid:
                continue
            self.account_combo.addItem(combo_display_name_for_profile(entry), pid)
            if pid == active:
                selected = self.account_combo.count() - 1
        if selected >= 0:
            self.account_combo.setCurrentIndex(selected)
        has = self.account_combo.count() > 0
        self.merge_btn.setEnabled(has)
        self.replace_btn.setEnabled(has)

    def _accept_mode(self, mode: str) -> None:
        pid = str(self.account_combo.currentData() or "").strip()
        if not pid:
            return
        self._result = (pid, mode)
        self.accept()

    def selected_import(self) -> tuple[str, str] | None:
        return self._result
