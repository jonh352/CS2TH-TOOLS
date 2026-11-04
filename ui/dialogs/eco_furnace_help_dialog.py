"""ECO 合炉配置与发布的四页图文指引（已脱敏截图）。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QScrollArea, QFrame

from config import ASSETS_DIR
from ui.dialog_topmost_support import apply_frameless_modal_geometry, install_dialog_topmost_follow_parent
from ui.modal_shell import build_frameless_modal_content, wire_overlay_dismiss


class EcoFurnaceHelpDialog(QDialog):
    STEPS = (
        ("1. 打开「我的」", "打开 ECO APP，点击底部导航栏右侧红框标出的「我的」。"),
        ("2. 进入设置", "在「我的」页点击右上角红框标出的设置图标。"),
        ("3. 打开「账号与安全」", "在设置页点击红框标出的「账号与安全」。"),
        ("4. 申请开放能力", "点击红框标出的「开放能力申请」，按 ECO 要求提交资料并等待审核。通过后复制身份 ID 到小助手，在小助手生成密钥，将公钥填入 ECO 的 RSA 公钥设置并开启签名验证、出售权限，最后保存小助手的 API 设置。个人模式无需配置回调，押金请在 ECO APP 缴纳。"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ECO合炉 · 图文指引")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.page_index = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close = build_frameless_modal_content(self, "ECO合炉 · 图文指引", box_width=800, include_message=False)
        root.addWidget(overlay)
        close.clicked.connect(self.accept)
        self.heading = QLabel()
        self.heading.setObjectName("recipeSavedTitle")
        layout.addWidget(self.heading)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(False)
        self.image_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.image_scroll.setFrameShape(QFrame.NoFrame)
        self.image_scroll.setMinimumHeight(400)
        self.image_scroll.setMaximumHeight(520)
        self.image_scroll.setWidget(self.image_label)
        layout.addWidget(self.image_scroll, 1)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(70)
        layout.addWidget(self.description)
        note = QLabel("ECO APP 界面以实际版本为准。")
        note.setObjectName("alchemyStep1Hint")
        layout.addWidget(note)
        footer = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.previous_button.clicked.connect(lambda: self._show_page(self.page_index-1))
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(lambda: self._show_page(self.page_index+1))
        self.done_button = QPushButton("知道了")
        self.done_button.clicked.connect(self.accept)
        for button in (self.previous_button, self.next_button, self.done_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setAutoDefault(False)
        footer.addWidget(self.previous_button)
        footer.addWidget(self.page_label, 1)
        footer.addWidget(self.next_button)
        footer.addWidget(self.done_button)
        layout.addLayout(footer)
        wire_overlay_dismiss(overlay, box, self, accept=True)
        install_dialog_topmost_follow_parent(self)
        self._show_page(0)

    def _show_page(self, page):
        self.page_index = max(0, min(len(self.STEPS)-1, page))
        title, description = self.STEPS[self.page_index]
        self.heading.setText(title)
        self.description.setText(description)
        pixmap = QPixmap(str(ASSETS_DIR / "help" / "eco" / f"step-{self.page_index+1}-redacted.webp"))
        scaled = pixmap.scaled(720, 500, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        self.image_label.setFixedSize(scaled.size())
        self.image_scroll.verticalScrollBar().setValue(0)
        self.image_label.setAccessibleName(title)
        self.page_label.setText(f"{self.page_index+1} / {len(self.STEPS)}")
        self.previous_button.setEnabled(self.page_index > 0)
        self.next_button.setVisible(self.page_index < len(self.STEPS)-1)
        self.done_button.setVisible(self.page_index == len(self.STEPS)-1)

    def showEvent(self, event):
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
