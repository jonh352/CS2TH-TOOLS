"""单条采购配方的账号修改与删除入口。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core.inventory_steam_accounts import list_profile_entries
from ui.dialog_topmost_support import apply_frameless_modal_geometry, install_dialog_topmost_follow_parent
from ui.modal_shell import build_frameless_modal_content, wire_overlay_dismiss


class EditPurchaseRecipeDialog(QDialog):
    def __init__(self, source_path: Path, batch: dict, entry: dict, batches: list, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("编辑采购配方")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.action = ""
        self._source_path = source_path
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close = build_frameless_modal_content(
            self, "编辑采购配方", box_width=560,
            message=f"当前账号：{batch.get('account_name') or 'Steam'}\n配方：{entry.get('title') or '采购配方'}",
        )
        root.addWidget(overlay)
        close.clicked.connect(self.reject)
        self.account_combo = QComboBox()
        accounts = {str(row.get("id") or ""): str(row.get("display_name") or "Steam") for row in list_profile_entries() if row.get("id")}
        for _path, payload in batches:
            profile = str(payload.get("profile_id") or "")
            if profile:
                accounts.setdefault(profile, str(payload.get("account_name") or "Steam"))
        for profile, label in accounts.items():
            self.account_combo.addItem(label, profile)
        index = self.account_combo.findData(str(batch.get("profile_id") or ""))
        self.account_combo.setCurrentIndex(max(0, index))
        account_label = QLabel("目标账号")
        layout.addWidget(account_label)
        layout.addWidget(self.account_combo)
        note = QLabel("同一 Steam 账号转移保留入库记录；更换账号后会撤销旧账号库存匹配，保留已买状态，请在新账号下重新核对库存。")
        note.setObjectName("alchemyStep1Hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        delete = QPushButton("删除该配方")
        delete.setObjectName("alchemyClearFileBtn")
        delete.clicked.connect(lambda: self._finish("delete"))
        buttons.addWidget(delete)
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        move = QPushButton("保存账号")
        move.setObjectName("confirmDialogOkBtn")
        move.setEnabled(self.account_combo.count() > 0)
        move.clicked.connect(lambda: self._finish("move"))
        buttons.addWidget(move)
        if entry.get("tradeup_completed"):
            account_label.hide()
            self.account_combo.hide()
            move.hide()
            note.setText("删除此配方不会删除其他炉的采购记录。")
        layout.addLayout(buttons)
        wire_overlay_dismiss(overlay, box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

    def _finish(self, action):
        self.action = action
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
