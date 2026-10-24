"""Steam 登录报错：UU 路由模式的两页图文操作说明。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from config import ASSETS_DIR
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.modal_shell import build_frameless_modal_content, wire_overlay_dismiss


class SteamLoginHelpDialog(QDialog):
    STEPS = (
        ("① 点击 UU 中 Steam 加速卡片右上角的「区服/节点」。", "uu-region-node.png"),
        ("② 选择「路由模式」，再点击「立即加速」即可。", "uu-routing-mode.png"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录报错 · UU 开启路由模式")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.page_index = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, self.box, layout, close_button = build_frameless_modal_content(
            self, "登录报错 · UU 开启路由模式", box_width=700,
            message="使用 UU 加速时遇到 Steam 登录报错，可按以下步骤切换路由模式。",
        )
        root.addWidget(overlay)
        close_button.clicked.connect(self.accept)
        self.step_label = QLabel()
        self.step_label.setWordWrap(True)
        layout.addWidget(self.step_label)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setFixedSize(644, 400)
        layout.addWidget(self.image_label, 0, Qt.AlignCenter)
        footer = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.previous_button.setObjectName("confirmDialogCancelBtn")
        self.previous_button.clicked.connect(lambda: self._show_page(self.page_index - 1))
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_button = QPushButton("下一页")
        self.next_button.setObjectName("confirmDialogOkBtn")
        self.next_button.clicked.connect(lambda: self._show_page(self.page_index + 1))
        self.done_button = QPushButton("知道了")
        self.done_button.setObjectName("confirmDialogOkBtn")
        self.done_button.clicked.connect(self.accept)
        for button in (self.previous_button, self.next_button, self.done_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setAutoDefault(False)
        footer.addWidget(self.previous_button)
        footer.addWidget(self.page_label, 1)
        footer.addWidget(self.next_button)
        footer.addWidget(self.done_button)
        layout.addLayout(footer)
        wire_overlay_dismiss(overlay, self.box, self, accept=True)
        install_dialog_topmost_follow_parent(self)
        self._show_page(0)

    def _show_page(self, index: int) -> None:
        self.page_index = max(0, min(len(self.STEPS) - 1, index))
        caption, filename = self.STEPS[self.page_index]
        self.step_label.setText(caption)
        pixmap = QPixmap(str(ASSETS_DIR / "help" / filename))
        self.image_label.setPixmap(pixmap.scaled(
            min(pixmap.width(), self.image_label.width()),
            min(pixmap.height(), self.image_label.height()),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        self.image_label.setAccessibleName(caption)
        self.page_label.setText(f"{self.page_index + 1} / {len(self.STEPS)}")
        self.previous_button.setEnabled(self.page_index > 0)
        last_page = self.page_index == len(self.STEPS) - 1
        self.next_button.setVisible(not last_page)
        self.done_button.setVisible(last_page)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
