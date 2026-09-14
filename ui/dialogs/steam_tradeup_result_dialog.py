"""Cinematic two-dimensional reveal of the actual Steam trade-up result."""

import math

from PySide6.QtCore import QEvent, QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QPushButton

from core.alchemy_quality import get_pid_map, get_template_from_goods_name
from core.data_utils import tradeup_display_quality
from core.inventory_icons import weapon_image_path_from_skin_template
from core.collection_icons import collection_icon_path
from ui.dialog_topmost_support import apply_frameless_modal_geometry, install_dialog_topmost_follow_parent
from ui.weapon_card_image_area import line_and_tint_for_quality_cn
from ui.dialogs.tradeup_history_product_dialog import _ProductRevealCover


def _product_image_path(product: dict) -> str | None:
    template = get_pid_map().get(str(product.get("paint_index") or ""))
    if template is None:
        template = get_template_from_goods_name(str(product.get("name") or ""))
    return weapon_image_path_from_skin_template(template) if template is not None else None


def _product_source_names(product: dict, template=None) -> str:
    """Use catalogue provenance without guessing a case from the input items."""
    if template is None:
        template = get_pid_map().get(str(product.get("paint_index") or ""))
    if template is None:
        template = get_template_from_goods_name(str(product.get("name") or ""))
    names = list(template.weapon_box_name or []) if template is not None else []
    if not names:
        for key in ("collection_name", "collection", "weapon_box_name", "weapon_box", "case_name"):
            value = product.get(key)
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, (list, tuple)):
                names.extend(item for item in value if isinstance(item, str))
    return " · ".join(dict.fromkeys(name.strip() for name in names if name.strip())) or "所属武器箱 / 收藏品待识别"


class SteamTradeupResultDialog(QDialog):
    """Reveal a confirmed result; animation never performs another trade-up."""

    def __init__(self, parent, products: list[dict], *, output_asset_ids=None, profit=None,
                 history_preview=False, image_path=None):
        super().__init__(parent)
        self.product = next((dict(row) for row in products if isinstance(row, dict)), {})
        self.profit = profit
        self.output_asset_ids = list(output_asset_ids or [])
        self._pixmap = QPixmap(image_path or _product_image_path(self.product) or "")
        self._history_preview = history_preview
        # Paint the complete result from the first frame. The opaque cover alone
        # conceals it, so every exposed area remains visible while dragging.
        self._progress = 1.0
        self._started = False
        self._finished = False
        self.setWindowTitle("汰换成功")
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.resize(1000, 700)
        anchor = parent.window() if parent is not None else None
        self._backdrop = anchor.grab() if anchor is not None and anchor.isVisible() else QPixmap()
        template = get_pid_map().get(str(self.product.get("paint_index") or ""))
        if template is None:
            template = get_template_from_goods_name(str(self.product.get("name") or ""))
        self._source_names = _product_source_names(self.product, template)
        sources = list(template.weapon_box_name or []) if template is not None else self._source_names.split(" · ")
        # A shared knife finish may belong to multiple cases: do not present one
        # case's badge as the sole provenance in that situation.
        icon_path = collection_icon_path(sources[0]) if len(sources) == 1 else None
        self._source_icon = QPixmap(str(icon_path)) if icon_path else QPixmap()
        color = str(self.product.get("rarity_color") or "#a879ef")
        if color and not color.startswith("#"):
            color = "#" + color
        self._quality = "汰换产物"
        if template is not None:
            self._quality = tradeup_display_quality(template)
            color, _ = line_and_tint_for_quality_cn(self._quality)
        self._grade = QColor(color)
        if not self._grade.isValid():
            self._grade = QColor("#a879ef")
        self._continue = QPushButton("继续", self)
        self._continue.hide()
        self._continue.setCursor(Qt.CursorShape.PointingHandCursor)
        self._continue.setStyleSheet("""
            QPushButton { color: #edf2f5; background: #29343d; border: 1px solid #53606b;
                border-radius: 3px; font-size: 14px; padding: 0px; min-width: 0px; min-height: 0px; }
            QPushButton:hover { background: #3b4a55; border-color: #adbdc8; }
            QPushButton:focus { border-color: #e1e8ec; }
        """)
        self._continue.clicked.connect(self._advance)
        self._details_popup = QLabel(self)
        self._details_popup.setTextFormat(Qt.TextFormat.RichText)
        self._details_popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._details_popup.setStyleSheet("""
            QLabel { background: #1b242e; color: #e6edf3; border: 1px solid #526170;
                border-radius: 8px; padding: 14px 18px; font-size: 14px; }
        """)
        self._details_popup.hide()
        self._continue.installEventFilter(self)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(2400)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.Linear)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(self._finish_reveal)
        self._cover_target = QFrame(self)
        self._cover_target.hide()
        self._reveal_cover = _ProductRevealCover(self, self._cover_target)
        self._reveal_cover.setStyleSheet("""
            QFrame#productRevealCover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #344653, stop:1 #1c2832);
                border: 1px solid #657c8d; border-radius: 0px;
            }
            QLabel { background: transparent; border: none; }
        """)
        self._reveal_cover.revealCompleted.connect(self._start_reveal)
        self._close_preview = QPushButton("×", self)
        self._close_preview.setFixedSize(30, 30)
        self._close_preview.setStyleSheet("background: transparent; color: #ddd; border: none; padding: 0; font-size: 24px;")
        self._close_preview.clicked.connect(self.accept)
        self._close_preview.setVisible(history_preview)
        self._close_preview.installEventFilter(self)
        if history_preview:
            self._finish_reveal()
        install_dialog_topmost_follow_parent(self)

    def _start_reveal(self):
        if self._started:
            return
        self._finish_reveal()

    def _set_progress(self, value):
        self._progress = float(value)
        self.update()

    def _finish_reveal(self):
        self._animation.stop()
        self._started = True
        self._reveal_cover.revealed = True
        self._reveal_cover.hide()
        self._progress = 1.0
        self._finished = True
        self._continue.setText("继续")
        if not self._history_preview:
            self._continue.show()
            self._continue.setFocus()
        self.update()

    def _advance(self):
        if self._finished:
            self.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._advance()
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
        self.setFocus()
        self._layout_cover()

    def _panel_scale(self):
        return 0.84 * min(self.width() / 1000, self.height() / 600)

    def _layout_cover(self):
        if not hasattr(self, "_reveal_cover"):
            return
        scale = self._panel_scale()
        button_width = max(64, round(96 * scale))
        button_height = max(28, round(34 * scale))
        inset = max(8, round(16 * scale))
        right = math.floor(self.width() / 2 + 450 * scale)
        bottom = math.floor(self.height() / 2 + 240 * scale)
        self._continue.setFixedSize(button_width, button_height)
        self._continue.move(right - inset - button_width, bottom - inset - button_height)
        if hasattr(self, "_close_preview"):
            self._close_preview.move(right - inset - 30, round(self.height() / 2 - 240 * scale) + inset)
        if not self._started:
            self._cover_target.setGeometry(math.floor(self.width() / 2 - 450 * scale),
                                           math.floor(self.height() / 2 - 240 * scale),
                                           math.ceil(900 * scale) + 2, math.ceil(480 * scale) + 2)
            self._reveal_cover.reset_geometry()
        self._continue.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_details_popup"):
            self._details_popup.hide()
        self._layout_cover()

    def done(self, result):
        self._animation.stop()
        super().done(result)

    def hideEvent(self, event):
        self._animation.stop()
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._continue or obj is getattr(self, "_close_preview", None):
            if event.type() == QEvent.Type.Enter and self._finished:
                self._show_details_popup()
            elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
                self._details_popup.hide()
        return super().eventFilter(obj, event)

    def _show_details_popup(self):
        if not self._finished:
            return
        try:
            wear = f"{float(self.product.get('float_value', self.product.get('float'))):.10f}"
        except (TypeError, ValueError):
            wear = "待刷新"
        amount, color = self._profit_display()
        self._details_popup.setText(
            '<table cellspacing="7" cellpadding="0">'
            f'<tr><td style="color:#9daab8">磨损</td><td>{wear}</td></tr>'
            '<tr><td style="color:#9daab8">盈亏</td>'
            f'<td style="color:{color};font-size:18px;font-weight:600">{amount}</td></tr></table>'
        )
        self._details_popup.adjustSize()
        self._details_popup.move(
            max(8, min(self.width() - self._details_popup.width() - 8,
                       self._continue.geometry().right() - self._details_popup.width())),
            max(8, self._continue.y() - self._details_popup.height() - 10))
        self._details_popup.show()
        self._details_popup.raise_()

    def _profit_display(self):
        try:
            value = float(self.profit)
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            return "待价格更新", "#9da5b0"
        if value > 0:
            return f"+ ¥{value:.2f}", "#6bdd9a"
        if value < 0:
            return f"− ¥{abs(value):.2f}", "#ff7878"
        return "¥0.00", "#c7ced6"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#202225"))
        if not self._backdrop.isNull():
            painter.drawPixmap(self.rect(), self._backdrop)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 175))
        painter.translate(self.width() / 2, self.height() / 2)
        painter.scale(self._panel_scale(), self._panel_scale())
        panel = QRectF(-450, -240, 900, 480)
        painter.setClipRect(panel)

        def tinted(factor, alpha=255):
            return QColor(round(self._grade.red() * factor), round(self._grade.green() * factor),
                          round(self._grade.blue() * factor), alpha)

        bg = QLinearGradient(0, -240, 0, 240)
        bg.setColorAt(0, tinted(0.065))
        bg.setColorAt(0.55, tinted(0.22))
        bg.setColorAt(1, tinted(0.20))
        painter.fillRect(panel, bg)
        glow = QRadialGradient(0, 0, 310)
        glow.setColorAt(0, tinted(0.9, 220))
        glow.setColorAt(0.13, tinted(0.65, 170))
        glow.setColorAt(0.5, tinted(0.38, 90))
        glow.setColorAt(1, tinted(0.15, 0))
        painter.fillRect(panel, glow)
        painter.save()
        watermark_font = QFont(self.font())
        watermark_font.setPixelSize(112)
        watermark_font.setBold(True)
        watermark_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 12)
        painter.setFont(watermark_font)
        painter.setPen(QColor(255, 255, 255, 14))
        painter.drawText(QRectF(-420, -100, 840, 200), Qt.AlignmentFlag.AlignCenter, "CS2TH")
        painter.restore()
        # Connected light points imitate the reference's angular background.
        points = [(-205, 75), (-180, 110), (-125, -150), (-20, -180),
                  (45, 30), (-40, 150), (215, 115), (320, -175), (445, 25)]
        edges = [(0, 1), (0, 2), (0, 4), (1, 5), (2, 3), (3, 4),
                 (4, 5), (4, 8), (5, 8), (6, 7), (7, 8), (1, 8)]
        painter.setPen(tinted(0.7, 55))
        for start, end in edges:
            painter.drawLine(QPointF(*points[start]), QPointF(*points[end]))
        for x, y in points:
            dot = QRadialGradient(x, y, 12)
            dot.setColorAt(0, tinted(1, 135))
            dot.setColorAt(1, tinted(0.8, 0))
            painter.fillRect(QRectF(x - 12, y - 12, 24, 24), dot)
        for index in range(22):
            angle = index * 2.39996
            painter.setPen(tinted(0.85, 30))
            painter.drawLine(QPointF(0, 0), QPointF(math.cos(angle) * 155,
                                                  math.sin(angle) * 155))
        painter.fillRect(QRectF(-450, -240, 12, 480), self._grade)

        def text(rect, value, size, color, bold=False):
            font = QFont(self.font())
            font.setPixelSize(size)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(QColor(color))
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                             | Qt.TextFlag.TextWordWrap, value)

        text(QRectF(-372, -207, 500, 58), "新物品", 42, self._grade)
        if not self._pixmap.isNull():
            fitted = self._pixmap.size().scaled(540, 235, Qt.AspectRatioMode.KeepAspectRatio)
            painter.drawPixmap(QRectF(-fitted.width() / 2, -fitted.height() / 2,
                                      fitted.width(), fitted.height()), self._pixmap, QRectF(self._pixmap.rect()))
        else:
            text(QRectF(-140, -90, 420, 90), "产物已生成 · 图片待刷新", 20, "#b8b8bb")
        from core.alchemy_quality import strip_appearance_suffix_from_goods_name
        name = strip_appearance_suffix_from_goods_name(str(self.product.get("name") or ""))
        source_center = (self._continue.y() + self._continue.height() / 2 - self.height() / 2) / self._panel_scale()
        details_offset = source_center - 155.5
        text(QRectF(-370, 73 + details_offset, 765, 52), name or "产物已生成，待刷新识别", 22, "#f1f1f1", True)
        source_left = -370
        if not self._source_icon.isNull():
            size = self._source_icon.size().scaled(52, 52, Qt.AspectRatioMode.KeepAspectRatio)
            painter.drawPixmap(QRectF(-370 + (52 - size.width()) / 2, source_center - size.height() / 2,
                                      size.width(), size.height()), self._source_icon, QRectF(self._source_icon.rect()))
            source_left += 66
        text(QRectF(source_left, source_center - 27.5, 310 - source_left, 47), self._source_names, 16, "#bcb8c0")
        painter.end()
