"""Play the unmodified CS2 new-item background through Qt's video decoder."""

from PySide6.QtCore import QObject, QRectF, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

from config import ASSETS_DIR


RARITY_COLORS = {
    "消费级": "#b0c3d9", "工业级": "#5e98d9", "军规级": "#4b69ff",
    "受限": "#8847ff", "保密": "#d32ce6", "隐秘": "#eb4b4b", "非凡": "#e4ae39",
}
_RARITY_NAMES = {
    "common": "消费级", "uncommon": "工业级", "rare": "军规级", "mythical": "受限",
    "legendary": "保密", "ancient": "隐秘", "immortal": "非凡", "contraband": "非凡",
    "1": "消费级", "2": "工业级", "3": "军规级", "4": "受限", "5": "保密", "6": "隐秘", "7": "非凡",
    "consumer grade": "消费级", "industrial grade": "工业级", "mil-spec grade": "军规级",
    "restricted": "受限", "classified": "保密", "covert": "隐秘", "extraordinary": "非凡",
}


def result_background_color(product, template=None):
    from core.data_utils import tradeup_display_quality
    quality = tradeup_display_quality(template) if template is not None else ""
    raw = str(product.get("quality") or product.get("rarity") or "").strip()
    quality = quality or _RARITY_NAMES.get(raw.lower(), raw)
    if quality == "非凡" or "★" in str(product.get("name") or ""):
        return "非凡", QColor(RARITY_COLORS["非凡"])
    if quality in RARITY_COLORS:
        return quality, QColor(RARITY_COLORS[quality])
    explicit = str(product.get("rarity_color") or "").strip()
    if explicit:
        color = QColor(explicit if explicit.startswith("#") else "#" + explicit)
        if color.isValid():
            return quality or "汰换产物", color
    return quality or "汰换产物", QColor(RARITY_COLORS.get(quality, "#9ca3af"))


class TradeupOriginalBackground(QObject):
    frame_changed = Signal()

    def __init__(self, grade: QColor, parent=None):
        super().__init__(parent)
        self.grade = QColor(grade)
        self.frame = QPixmap()
        self.sink = QVideoSink(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoSink(self.sink)
        self.player.setLoops(QMediaPlayer.Infinite)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.setSource(QUrl.fromLocalFile(str(ASSETS_DIR / "reveal" / "acknowledge540p.webm")))

    def _on_frame(self, video_frame):
        image = video_frame.toImage()
        if image.isNull():
            return
        # Keep the decoder frame unchanged. Tint in the final paint pass instead
        # of allocating and painting another full-size pixmap for every frame.
        self.frame = QPixmap.fromImage(image)
        self.frame_changed.emit()

    def start(self):
        self.player.play()

    def stop(self):
        self.player.stop()

    def paint(self, painter, panel):
        painter.fillRect(panel, QColor("#111317"))
        if not self.frame.isNull():
            painter.drawPixmap(panel, self.frame, QRectF(self.frame.rect()))
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode_Multiply)
            painter.fillRect(panel, self.grade)
            painter.restore()
