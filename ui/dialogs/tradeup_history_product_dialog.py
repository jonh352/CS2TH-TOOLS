"""Large, focused product preview for trade-up history."""

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QGraphicsDropShadowEffect, QLabel, QVBoxLayout

from core.alchemy_quality import resolve_inventory_skin_template
from core.data_utils import tradeup_display_quality
from ui.dialog_topmost_support import apply_frameless_modal_geometry, install_dialog_topmost_follow_parent
from ui.modal_shell import build_frameless_modal_content
from ui.weapon_card_image_area import line_and_tint_for_quality_cn


class _WatermarkedProductImage(QLabel):
    """Paint branding beneath the transparent product image."""

    def paintEvent(self, event) -> None:
        # QLabel paints the styled background and fallback text as usual.
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont(self.font())
        font.setPixelSize(min(110, max(24, self.width() // 4)))
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 9)
        painter.setFont(font)
        painter.setPen(QColor(105, 133, 157, 24))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "CS2TH")
        # Redraw the product above the watermark so its opaque pixels stay clear.
        pixmap = self.pixmap()
        if pixmap is not None and not pixmap.isNull():
            size = pixmap.deviceIndependentSize()
            painter.drawPixmap(
                round((self.width() - size.width()) / 2),
                round((self.height() - size.height()) / 2), pixmap,
            )
        painter.end()


class _ProductRevealCover(QFrame):
    """An opaque cover that reveals once at least two thirds leaves the card."""

    revealCompleted = Signal()

    def __init__(self, parent, target):
        super().__init__(parent)
        self.target = target
        self.revealed = False
        self._drag_origin = None
        self.setObjectName("productRevealCover")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet("""
            QFrame#productRevealCover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #ecf3f9, stop:1 #cdddea);
                border: 1px solid #bbcede; border-radius: 22px;
            }
            QLabel { background: transparent; border: none; }
        """)
        layout = QVBoxLayout(self)
        layout.addStretch()
        brand = QLabel("CS2TH")
        brand.setStyleSheet("color: #a9c0d3; font-size: 76px; font-weight: 700;")
        hint = QLabel("拖动揭晓产物")
        hint.setStyleSheet("color: #708ba2; font-size: 15px;")
        for label in (brand, hint):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        layout.addStretch()

    def target_rect(self):
        return QRect(self.target.mapTo(self.parentWidget(), QPoint()), self.target.size())

    def reset_geometry(self):
        if not self.revealed:
            self.setGeometry(self.target_rect())
            self.raise_()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_origin is None:
            return
        self.move(event.globalPosition().toPoint() - self._drag_origin)
        overlap = self.geometry().intersected(self.target_rect())
        if overlap.width() * overlap.height() * 3 <= self.width() * self.height():
            self.revealed = True
            self._drag_origin = None
            self.releaseMouse()
            self.hide()
            self.revealCompleted.emit()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        event.accept()


class TradeupHistoryProductDialog(QDialog):
    def __init__(self, parent, product: dict, profit, image_path: str, *, reveal: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("汰换产物")
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close = build_frameless_modal_content(
            self, "", box_width=540, box_object_name="historyProductPreviewBox",
            overlay_object_name="historyProductPreviewOverlay",
        )
        overlay.setStyleSheet("QWidget#historyProductPreviewOverlay { background: transparent; }")
        layout.setContentsMargins(28, 18, 28, 26)
        layout.setSpacing(10)
        box.setStyleSheet("""
            QFrame#historyProductPreviewBox {
                background: qlineargradient(x1:0, y1:0, x2:0.4, y2:1,
                    stop:0 #f0f5fa, stop:1 #e5edf4);
                border: 1px solid #c9d8e5;
                border-radius: 22px;
            }
            QLabel { background: transparent; border: none; color: #60758a; }
            QPushButton#loginCloseBtn {
                background: transparent; color: #7b8da1;
                border: none; border-radius: 14px;
                padding: 0px; font-size: 20px;
            }
            QPushButton#loginCloseBtn:hover { background: #dce6ef; color: #263e56; }
        """)
        shadow = QGraphicsDropShadowEffect(box)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(35, 60, 85, 45))
        box.setGraphicsEffect(shadow)
        close.setFixedSize(28, 28)
        close.setText("×")
        close.setToolTip("关闭")
        root.addWidget(overlay)
        close.clicked.connect(self.accept)

        image = _WatermarkedProductImage()
        image.setFixedHeight(225)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(image_path) if image_path else QPixmap()
        if pixmap.isNull():
            image.setText("暂无图片")
        else:
            image.setPixmap(pixmap.scaled(465, 225, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)

        name = str(product.get("name") or "未知饰品")
        template = resolve_inventory_skin_template(dict(product, market_name=name, market_hash_name=name))
        color = str(product.get("rarity_color") or "")
        if template is not None:
            color, _ = line_and_tint_for_quality_cn(tradeup_display_quality(template))
        if not QColor(color).isValid():
            color = "#94a3b8"
        # A restrained grade-colored glow gives the image depth without a heavy panel.
        grade = QColor(color)
        image.setStyleSheet(
            "border-radius: 18px; background: qradialgradient("
            "cx:0.5, cy:0.52, radius:0.62, fx:0.5, fy:0.52, "
            f"stop:0 rgba({grade.red()}, {grade.green()}, {grade.blue()}, 26), "
            "stop:0.65 rgba(226, 236, 245, 70), stop:1 rgba(240, 245, 250, 0));"
        )
        name_label = QLabel(name)
        name_label.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 600;")
        name_label.setWordWrap(True)
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_label)

        try:
            wear = f"磨损：{float(product.get('float_value', product.get('float'))):.10f}"
        except (TypeError, ValueError):
            wear = "磨损：待刷新"
        wear_label = QLabel(wear)
        wear_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wear_label.setStyleSheet("color: #73869a; font-size: 13px; padding-bottom: 6px;")
        wear_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(wear_label)

        if profit is None:
            text, color = "盈亏：待产物价格更新", "#94a3b8"
        else:
            value = float(profit)
            text = f"{'盈利' if value > 0 else '亏损' if value < 0 else '持平'} ¥{abs(value):.2f}"
            color = "#059669" if value >= 0 else "#dc4c58"
        result = QLabel(text)
        result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result.setStyleSheet(
            f"color: {color}; font-size: 23px; font-weight: 600; padding: 12px 18px 0px;"
            "background: transparent; border: none; border-top: 1px solid #d3dfe9;"
        )
        layout.addWidget(result)
        image.setObjectName("steamTradeupResultImage")
        name_label.setObjectName("steamTradeupResultName")
        wear_label.setObjectName("steamTradeupResultMeta")
        result.setObjectName("steamTradeupResultProfit")
        self._reveal_cover = _ProductRevealCover(self, box) if reveal else None
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
        self.layout().activate()
        if self._reveal_cover is not None:
            self._reveal_cover.reset_geometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cover = getattr(self, "_reveal_cover", None)
        if cover is not None:
            self.layout().activate()
            cover.reset_geometry()
