"""自定义浮点数输入框"""

import math
import struct

from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QWheelEvent


def format_float_shortest(v: float) -> str:
    """最短显示：去掉小数部分多余的 0"""
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


class FloatLineEdit(QLineEdit):
    """自定义浮点数输入框：自由输入、最短显示、支持中文句号、失焦时校验"""

    valueChanged = Signal(float)
    errorMessageChanged = Signal(str)

    def __init__(self, min_val: float = 0.0, max_val: float = 1.0, parent=None):
        super().__init__(parent)
        self._min_val = min_val
        self._max_val = max_val
        self._value = min_val
        self._last_valid_value = min_val
        self.setPlaceholderText(f"{min_val} ~ {max_val}")
        self.setText(format_float_shortest(min_val))
        self.editingFinished.connect(self._on_editing_finished)
        self.textEdited.connect(self._normalize_decimal_punctuation_live)
        self.installEventFilter(self)

    def setRange(self, min_val: float, max_val: float):
        self._min_val = min_val
        self._max_val = max_val
        self.setPlaceholderText(f"{min_val} ~ {max_val}")

    def setValue(self, v: float):
        v = max(self._min_val, min(self._max_val, v))
        self._value = v
        self._last_valid_value = v
        self.blockSignals(True)
        self.setText(format_float_shortest(v))
        self.blockSignals(False)

    def value(self) -> float:
        return self._value

    def wear_input_string(self) -> str:
        """当前文本若可解析且在范围内则原样返回（已 strip、中文句号转英文），否则退回上次合法值的短字符串。"""
        raw = self.text().strip().replace("。", ".")
        ok, _v, _err = self._parse_text()
        if ok:
            return raw
        return format_float_shortest(self._last_valid_value)

    def apply_display_text(self, raw: str) -> bool:
        """设置显示文本并在可解析且落在范围内时同步内部值；用于程序预填（如从特殊磨损页带入）。"""
        s = (raw or "").strip().replace("。", ".")
        self.blockSignals(True)
        self.setText(s)
        ok, v, _err = self._parse_text()
        if ok:
            if v != self._value:
                self._value = v
                self._last_valid_value = v
            self.errorMessageChanged.emit("")
            self.blockSignals(False)
            return True
        self.blockSignals(False)
        return False

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Type.KeyPress:
            if event.text() == "。":
                self.insert(".")
                return True
        return super().eventFilter(obj, event)

    def _normalize_decimal_punctuation_live(self, text: str) -> None:
        """用户编辑时将中文句号/全角点即时替换为英文小数点，保证显示文本一致。"""
        if "。" not in text and "．" not in text:
            return
        pos = self.cursorPosition()
        normalized = text.replace("。", ".").replace("．", ".")
        if normalized == text:
            return
        self.blockSignals(True)
        self.setText(normalized)
        self.blockSignals(False)
        self.setCursorPosition(min(pos, len(normalized)))

    def _parse_text(self) -> tuple[bool, float, str]:
        """解析文本，返回 (是否有效, 数值, 错误信息)"""
        raw = self.text().strip().replace("。", ".")
        if not raw:
            return False, 0.0, "请输入数值"
        try:
            v = float(raw)
        except ValueError:
            return False, 0.0, "请输入有效的数字"
        if v < self._min_val or v > self._max_val:
            return False, v, f"该皮肤磨损度在 {self._min_val} ~ {self._max_val} 之间"
        return True, v, ""

    def _on_editing_finished(self):
        raw = self.text().strip().replace("。", ".")
        ok, v, err = self._parse_text()
        if ok:
            if v != self._value:
                self._value = v
                self._last_valid_value = v
                self.valueChanged.emit(v)
            # 保留用户写法（如小数末尾的 0），不再用 format_float_shortest 压成最短形式
            self.blockSignals(True)
            self.setText(raw)
            self.blockSignals(False)
            self.errorMessageChanged.emit("")
        else:
            self.blockSignals(True)
            self.setText(format_float_shortest(self._last_valid_value))
            self.blockSignals(False)
            self.errorMessageChanged.emit(err)


def _try_parse_float_for_preview(raw: str) -> float | None:
    s = (raw or "").strip().replace("。", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _ieee754_binary32_decimal_digits(v: float) -> str:
    """IEEE754 binary32 舍入后的十进制；固定小数位，避免 ``g`` 格式的科学计数法。"""
    x = float(v)
    b32 = struct.pack(">f", x)
    f32 = struct.unpack(">f", b32)[0]
    return f"{f32:.18f}"


class WearFloatLineEditWithIeee(QWidget):
    """FloatLineEdit；聚焦输入时以顶层浮动框显示 IEEE754 binary32 实际十进制（仅数字）。"""

    valueChanged = Signal(float)
    errorMessageChanged = Signal(str)

    def __init__(self, min_val: float = 0.0, max_val: float = 1.0, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._edit = FloatLineEdit(min_val, max_val, self)
        self._edit.valueChanged.connect(self.valueChanged.emit)
        self._edit.errorMessageChanged.connect(self.errorMessageChanged.emit)
        self._edit.textChanged.connect(self._on_edit_text_changed)
        self._edit.installEventFilter(self)

        self._popup: QFrame | None = None
        self._popup_label: QLabel | None = None
        # 弹窗用 Qt.Tool + 全局坐标定位；主窗口拖动时子控件不会收到 Move，须在顶层窗上监听
        self._ieee_filter_window: QWidget | None = None
        self._focus_out_timer = QTimer(self)
        self._focus_out_timer.setSingleShot(True)
        self._focus_out_timer.setInterval(0)
        self._focus_out_timer.timeout.connect(self._hide_popup_if_edit_not_focused)

        lay.addWidget(
            self._edit,
            0,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        # 外壳不参与焦点链；勿用 self.setFocusPolicy：子类已重写为只改 _edit，此时会 AttributeError
        super().setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _ensure_popup(self) -> None:
        if self._popup is not None:
            return
        # Qt.Tool：浮动在父窗口之上，不被滚动区裁剪；无提示文案，仅数字
        self._popup = QFrame(self, Qt.Tool | Qt.FramelessWindowHint)
        self._popup.setObjectName("alchemyWearIeeeFrame")
        self._popup.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        fl = QVBoxLayout(self._popup)
        fl.setContentsMargins(8, 6, 8, 6)
        fl.setSpacing(0)
        self._popup_label = QLabel(self._popup)
        self._popup_label.setObjectName("alchemyWearIeeeLabel")
        self._popup_label.setWordWrap(False)
        self._popup_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        mono = QFont("Consolas")
        if not mono.exactMatch():
            mono = QFont("Courier New")
        if not mono.exactMatch():
            mono = QFont("monospace")
        self._popup_label.setFont(mono)
        fl.addWidget(self._popup_label)

    def _ensure_top_level_window_event_filter(self) -> None:
        win = self.window()
        if win is None or win is self._ieee_filter_window:
            return
        if self._ieee_filter_window is not None:
            self._ieee_filter_window.removeEventFilter(self)
        self._ieee_filter_window = win
        win.installEventFilter(self)

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self._ensure_top_level_window_event_filter()

    def _position_popup(self) -> None:
        if self._popup is None:
            return
        self._popup.adjustSize()
        gap = 2
        pos = self._edit.mapToGlobal(QPoint(0, self._edit.height() + gap))
        self._popup.move(pos)

    def _show_or_update_popup(self) -> None:
        if not self._edit.hasFocus():
            return
        v = _try_parse_float_for_preview(self._edit.text())
        if v is None:
            self._hide_popup()
            return
        self._ensure_popup()
        assert self._popup_label is not None
        self._popup_label.setText(_ieee754_binary32_decimal_digits(v))
        self._position_popup()
        self._popup.show()
        self._popup.raise_()

    def _hide_popup(self) -> None:
        if self._popup is not None:
            self._popup.hide()

    def _hide_popup_if_edit_not_focused(self) -> None:
        fw = QApplication.focusWidget()
        if fw is not self._edit:
            self._hide_popup()

    def _on_edit_text_changed(self, _t: str) -> None:
        if self._edit.hasFocus():
            self._show_or_update_popup()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._edit:
            et = event.type()
            if et == QEvent.Type.FocusIn:
                self._focus_out_timer.stop()
                self._show_or_update_popup()
            elif et == QEvent.Type.FocusOut:
                self._focus_out_timer.start()
            elif et in (QEvent.Type.Move, QEvent.Type.Resize):
                if self._popup is not None and self._popup.isVisible():
                    self._position_popup()
        elif obj is self._ieee_filter_window and event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
        ):
            if self._popup is not None and self._popup.isVisible():
                self._position_popup()
        return super().eventFilter(obj, event)

    def hideEvent(self, event: QEvent) -> None:
        self._hide_popup()
        super().hideEvent(event)

    def line_edit(self) -> FloatLineEdit:
        return self._edit

    def setRange(self, min_val: float, max_val: float) -> None:
        self._edit.setRange(min_val, max_val)

    def setValue(self, v: float) -> None:
        self._edit.setValue(v)

    def value(self) -> float:
        return self._edit.value()

    def wear_input_string(self) -> str:
        return self._edit.wear_input_string()

    def apply_display_text(self, raw: str) -> bool:
        ok = self._edit.apply_display_text(raw)
        if self._edit.hasFocus():
            self._show_or_update_popup()
        return ok

    def setReadOnly(self, ro: bool) -> None:
        self._edit.setReadOnly(ro)

    def setFocusPolicy(self, policy: Qt.FocusPolicy) -> None:
        self._edit.setFocusPolicy(policy)

    def clearFocus(self) -> None:
        self._edit.clearFocus()

    def blockSignals(self, b: bool) -> bool:
        return self._edit.blockSignals(b)

    def setFixedWidth(self, w: int) -> None:
        super().setFixedWidth(w)
        self._edit.setFixedWidth(w)
