"""Interactive offline demo of the real result dialog; never contacts Steam."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget
from ui.dialogs.steam_tradeup_result_dialog import SteamTradeupResultDialog


class RevealDemo(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CS2TH · 汰换揭晓体验（模拟）")
        self.resize(1100, 780)
        self.setMinimumSize(720, 520)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet("""
            QWidget { background: #17212a; color: #e9eff4; }
            QLabel { background: transparent; }
            QPushButton { background: #354b5b; border: 1px solid #6b8597;
                border-radius: 4px; padding: 14px 32px; font-size: 16px; }
            QPushButton:hover { background: #466175; }
            QPushButton:disabled { color: #81909b; }
            QProgressBar { border: none; background: #27343f; border-radius: 3px; }
            QProgressBar::chunk { background: #a879ef; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(60, 50, 60, 50)
        layout.setSpacing(22)
        layout.addStretch()
        title = QLabel("体验一次汰换揭晓")
        title.setStyleSheet("font-size: 30px; font-weight: 600;")
        subtitle = QLabel("材料确认 → 合成等待 → 拖动遮罩，逐步露出产物")
        note = QLabel("离线演示 · 不连接 Steam · 不消耗库存 · 不写入采购记录")
        note.setStyleSheet("color: #90a4b3; font-size: 13px;")
        for label in (title, subtitle, note):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        self.status = QLabel("点击开始，体验完整过程；结束后可以再次播放。")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.button = QPushButton("开始模拟汰换")
        self.button.clicked.connect(self.start)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(650)
        self.timer.timeout.connect(self.step)
        self.stage = 0

    def start(self):
        self.stage = 0
        self.button.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("正在确认模拟材料…")
        self.timer.start()

    def step(self):
        self.stage += 1
        self.progress.setValue(min(100, self.stage * 25))
        self.status.setText({1: "模拟材料已就绪", 2: "正在模拟合成…", 3: "合成完成，即将揭晓…"}.get(self.stage, "正在展示模拟产物"))
        if self.stage < 4:
            return
        self.timer.stop()
        dialog = SteamTradeupResultDialog(self, [{
            "name": "AK-47 | 传承 (崭新出厂)", "float_value": 0.0327814592,
            "rarity_color": "eb4b4b",
        }], profit=128.5)
        # Use the same local artwork even when the catalogue uses another locale.
        from PySide6.QtGui import QPixmap
        art = Path(__file__).resolve().parents[1] / "weapon_images/weapon_ak47_ak_porcelain_light_png.webp"
        if art.is_file():
            dialog._pixmap = QPixmap(str(art))
        dialog.exec()
        dialog.deleteLater()
        self.status.setText("演示结束。刚才的产物、磨损和盈亏均为示例，可再次播放。")
        self.button.setText("再体验一次")
        self.button.setEnabled(True)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = RevealDemo()
    window.show()
    sys.exit(app.exec())


