"""分段互斥按钮 + 底层滑动高亮块（炼金模式切换、数据采集配方/自定义等）。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEasingCurve, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget


class SegmentedCheckSwitch(QWidget):
    _ANIM_MS = 220

    def __init__(
        self,
        parent=None,
        *,
        container_object_name: str,
        slider_object_name: str,
        segments: Sequence[tuple[str, str]],
    ) -> None:
        super().__init__(parent)
        if len(segments) < 2:
            raise ValueError("SegmentedCheckSwitch 至少需要 2 个分段")
        self.setObjectName(container_object_name)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._slider = QFrame(self)
        self._slider.setObjectName(slider_object_name)
        self._slider.hide()
        self._slider.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._buttons: list[QPushButton] = []
        for i, (obj_name, label) in enumerate(segments):
            b = QPushButton(label)
            b.setObjectName(obj_name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setCursor(Qt.PointingHandCursor)
            self._buttons.append(b)
            lay.addWidget(b)

        self._slider.lower()
        self._geom_anim: QVariantAnimation | None = None

    @property
    def buttons(self) -> list[QPushButton]:
        return self._buttons

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._geom_anim is not None:
            self._geom_anim.stop()
            self._geom_anim.deleteLater()
            self._geom_anim = None
        self._apply_slider_geometry(animate=False)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, lambda: self._apply_slider_geometry(False))

    def _checked_button(self) -> QPushButton:
        for b in self._buttons:
            if b.isChecked():
                return b
        return self._buttons[0]

    def _target_slider_rect(self) -> QRect:
        btn = self._checked_button()
        return QRect(btn.geometry())

    def sync_mode_slider(self, *, animate: bool = True) -> None:
        self._apply_slider_geometry(animate=animate)

    def _apply_slider_geometry(self, animate: bool) -> None:
        if self.width() <= 0 or self.height() <= 0:
            return
        target = self._target_slider_rect()
        if target.width() <= 0 or target.height() <= 0:
            return

        if self._geom_anim is not None:
            self._geom_anim.stop()
            self._geom_anim.deleteLater()
            self._geom_anim = None

        if not animate or not self._slider.isVisible():
            self._slider.setGeometry(target)
            self._slider.show()
            return

        start = self._slider.geometry()
        if start == target:
            return

        anim = QVariantAnimation(self)
        anim.setDuration(self._ANIM_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(target)

        def on_value(value) -> None:
            if isinstance(value, QRect):
                self._slider.setGeometry(value)

        def on_finished() -> None:
            if self._geom_anim is anim:
                self._geom_anim = None
            anim.deleteLater()
            self._slider.setGeometry(self._target_slider_rect())

        anim.valueChanged.connect(on_value)
        anim.finished.connect(on_finished)
        self._geom_anim = anim
        anim.start()
