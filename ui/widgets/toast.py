"""顶部提示条 - 滑入/淡入淡出"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QGraphicsOpacityEffect, QWidget
from PySide6.QtCore import (
    Qt,
    QPropertyAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QSequentialAnimationGroup,
    QPauseAnimation,
    QRect,
)
from PySide6.QtGui import QFontMetrics

from config import TITLE_BAR_HEIGHT, TOAST_TOP_MARGIN, TOAST_DURATION_MS, TOAST_ANIM_DURATION

# 与 theme/chrome.qss 中 toastWidget / toastWidgetSuccess / toastWidgetError 一致：border 1px + padding 12px 24px
_TOAST_FRAME_EXTRA_W = 2 + 24 + 24  # 左右 border + 水平 padding
_TOAST_FRAME_EXTRA_H = 2 + 12 + 12  # 上下 border + 垂直 padding


def show_toast(widget: QWidget | None, message: str, style: str = "default") -> bool:
    """通过页面 widget 获取主窗口并显示 toast。返回是否成功显示。"""
    win = widget.window() if widget else None
    if win and hasattr(win, "toast") and win.toast:
        win.toast.show_toast(message, style=style)
        return True
    return False


class ToastWidget(QFrame):
    """顶部提示条 - 标题栏下方，滑入 + 淡入淡出"""

    def __init__(
        self,
        parent=None,
        stay_below: QWidget | None = None,
        *,
        top_inset_px: int | None = None,
    ):
        super().__init__(parent)
        self._stay_below = stay_below
        # None：按自绘标题栏高度；0：内容区顶边起算（系统标题栏时 central 已在客户区内）
        self._top_inset_px = top_inset_px
        self.setObjectName("toastWidget")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(False)
        layout.addWidget(self.label, 0, Qt.AlignCenter)
        self._anim_group = None
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0)
        self.setGraphicsEffect(self._opacity_effect)

    def show_toast(self, message: str, style: str = "default"):
        """显示提示，2秒后自动退出。style: default / success / error / warning / info（success 绿、error 红，其余默认橙条）"""
        if style == "success":
            toast_name = "toastWidgetSuccess"
        elif style == "error":
            toast_name = "toastWidgetError"
        elif style == "warning":
            toast_name = "toastWidgetWarning"
        else:
            toast_name = "toastWidget"
        self.setObjectName(toast_name)
        parent = self.parent()
        if not parent:
            return

        self.style().unpolish(self)
        self.style().polish(self)
        self.label.setStyleSheet("")

        text = (message or "").replace("\r\n", " ").replace("\n", " ").strip() or " "
        fm = QFontMetrics(self.label.font())

        parent_side_margin = 24
        max_frame_w = max(80, parent.width() - parent_side_margin)
        max_inner_w = max(24, max_frame_w - _TOAST_FRAME_EXTRA_W)

        if fm.horizontalAdvance(text) > max_inner_w:
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max_inner_w)

        self.label.setText(text)
        text_w = fm.horizontalAdvance(text)
        toast_w = min(max_frame_w, text_w + _TOAST_FRAME_EXTRA_W)
        toast_h = fm.height() + _TOAST_FRAME_EXTRA_H

        self.setFixedSize(toast_w, toast_h)

        x = (parent.width() - self.width()) // 2
        inset = (
            TITLE_BAR_HEIGHT if self._top_inset_px is None else self._top_inset_px
        )
        y_visible = inset + TOAST_TOP_MARGIN
        y_hidden = -self.height() - 10

        if self._anim_group:
            self._anim_group.stop()
            self._anim_group.deleteLater()

        self.setGeometry(x, y_hidden, self.width(), self.height())
        self._opacity_effect.setOpacity(0)
        self.show()
        self.raise_()
        if self._stay_below:
            self._stay_below.raise_()

        anim_in_geo = QPropertyAnimation(self, b"geometry")
        anim_in_geo.setDuration(TOAST_ANIM_DURATION)
        anim_in_geo.setStartValue(QRect(x, y_hidden, self.width(), self.height()))
        anim_in_geo.setEndValue(QRect(x, y_visible, self.width(), self.height()))
        anim_in_geo.setEasingCurve(QEasingCurve.OutCubic)

        anim_in_opacity = QPropertyAnimation(self._opacity_effect, b"opacity")
        anim_in_opacity.setDuration(TOAST_ANIM_DURATION)
        anim_in_opacity.setStartValue(0.0)
        anim_in_opacity.setEndValue(1.0)
        anim_in_opacity.setEasingCurve(QEasingCurve.OutCubic)

        anim_in = QParallelAnimationGroup(self)
        anim_in.addAnimation(anim_in_geo)
        anim_in.addAnimation(anim_in_opacity)

        pause = QPauseAnimation()
        pause.setDuration(TOAST_DURATION_MS)

        anim_out_geo = QPropertyAnimation(self, b"geometry")
        anim_out_geo.setDuration(TOAST_ANIM_DURATION)
        anim_out_geo.setStartValue(QRect(x, y_visible, self.width(), self.height()))
        anim_out_geo.setEndValue(QRect(x, y_hidden, self.width(), self.height()))
        anim_out_geo.setEasingCurve(QEasingCurve.InCubic)

        anim_out_opacity = QPropertyAnimation(self._opacity_effect, b"opacity")
        anim_out_opacity.setDuration(TOAST_ANIM_DURATION)
        anim_out_opacity.setStartValue(1.0)
        anim_out_opacity.setEndValue(0.0)
        anim_out_opacity.setEasingCurve(QEasingCurve.InCubic)

        anim_out = QParallelAnimationGroup(self)
        anim_out.addAnimation(anim_out_geo)
        anim_out.addAnimation(anim_out_opacity)

        self._anim_group = QSequentialAnimationGroup(self)
        self._anim_group.addAnimation(anim_in)
        self._anim_group.addAnimation(pause)
        self._anim_group.addAnimation(anim_out)
        self._anim_group.finished.connect(self._on_anim_finished)
        self._anim_group.start()

    def _on_anim_finished(self):
        self.hide()
        self._anim_group = None
