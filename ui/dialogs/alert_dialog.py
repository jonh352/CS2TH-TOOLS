"""通用提示弹窗 - 与登录等弹窗风格一致"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.app_settings import save_alchemy_wear_step2_notice_dismissed
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)

_WEAR_INPUT_NOTICE_TEXT = (
    "我们会将你输入的磨损度转换为游戏中的数据格式，以便更准确地模拟实际效果，转换后的数值会显示在输入框下方。"
)


def _build_frameless_modal_content(
    dialog: QDialog,
    title: str,
    message: str,
    *,
    box_width: int = 400,
    box_object_name: str = "loginBox",
    message_object_name: str = "loginError",
) -> tuple[QWidget, QFrame, QVBoxLayout, QPushButton]:
    """蒙层 + 内容框 + 标题行 + 消息；返回 overlay、box、box 内主布局、关闭按钮（信号由调用方连接）。"""
    overlay = QWidget(dialog)
    overlay.setObjectName("alertOverlay")
    overlay.setAttribute(Qt.WA_StyledBackground)

    overlay_layout = QVBoxLayout(overlay)
    overlay_layout.setAlignment(Qt.AlignCenter)

    box = QFrame()
    box.setObjectName(box_object_name)
    box.setFixedWidth(box_width)

    layout = QVBoxLayout(box)
    layout.setContentsMargins(32, 32, 32, 32)
    layout.setSpacing(20)

    header = QHBoxLayout()
    title_label = QLabel(title)
    title_label.setObjectName("loginTitle")
    header.addWidget(title_label)
    header.addStretch()
    close_btn = QPushButton("✕")
    close_btn.setObjectName("loginCloseBtn")
    close_btn.setCursor(Qt.PointingHandCursor)
    close_btn.setAutoDefault(False)
    close_btn.setDefault(False)
    header.addWidget(close_btn)
    layout.addLayout(header)
    layout.addSpacing(4)

    msg_label = QLabel(message)
    msg_label.setObjectName(message_object_name)
    msg_label.setWordWrap(True)
    msg_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    inner_w = max(1, box_width - layout.contentsMargins().left() - layout.contentsMargins().right())
    msg_label.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Minimum,
    )
    msg_label.setMinimumWidth(inner_w)
    layout.addWidget(msg_label)

    overlay_layout.addWidget(box)
    return overlay, box, layout, close_btn


def _wire_overlay_dismiss(overlay: QWidget, box: QFrame, dialog: QDialog, *, accept: bool) -> None:
    """点击蒙层空白处关闭，行为与历史一致（Alert 用 accept，Confirm 用 reject）。"""

    def on_overlay_click(event):
        if event.button() == Qt.LeftButton:
            w = overlay.childAt(event.pos().x(), event.pos().y())
            if w is None or (w != box and not box.isAncestorOf(w)):
                (dialog.accept if accept else dialog.reject)()

    overlay.mousePressEvent = on_overlay_click


class AlertDialog(QDialog):
    """简单提示弹窗 - 标题 + 消息 + 确定按钮"""

    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close_btn = _build_frameless_modal_content(
            self,
            title,
            message,
            message_object_name="alertDialogMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.accept)

        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("loginSubmitBtn")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)

        _wire_overlay_dismiss(overlay, box, self, accept=True)
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())


class ConfirmDialog(QDialog):
    """确认弹窗 - 与 AlertDialog / 登录弹窗同壳：标题 + 说明 + 取消 / 确定。"""

    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close_btn = _build_frameless_modal_content(
            self,
            title,
            message,
            message_object_name="alertDialogMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("confirmDialogCancelBtn")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("confirmDialogOkBtn")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        _wire_overlay_dismiss(overlay, box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())


class ImportSubstrateToAlchemyDialog(QDialog):
    """数据采集 / 库存「导入炼金计算」：替换底物列表或在现有底物上追加。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = "导入炼金计算"
        message = (
            "请选择导入方式：\n\n"
            "「替换原底物」将清空当前炼金页的底物列表，仅保留本次导入的数据。\n\n"
            "「在现有基础上追加」将保留已有底物，并把本次导入合并进列表（与已有条目重复的皮肤与磨损不会重复添加）。"
        )
        self._mode: str = "merge"
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close_btn = _build_frameless_modal_content(
            self,
            title,
            message,
            box_width=480,
            message_object_name="alertDialogMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        # 与正文区视觉对齐：换行后文字块右侧常留白，按钮行略收进避免贴边过紧
        btn_row.setContentsMargins(0, 0, 12, 0)
        btn_row.addStretch(1)
        replace_btn = QPushButton("替换原底物")
        replace_btn.setObjectName("importToAlchemyReplaceBtn")
        replace_btn.setCursor(Qt.PointingHandCursor)
        replace_btn.setAutoDefault(False)
        replace_btn.setDefault(False)
        replace_btn.clicked.connect(self._on_replace)
        btn_row.addWidget(replace_btn)
        merge_btn = QPushButton("在现有基础上追加")
        merge_btn.setObjectName("importToAlchemyMergeBtn")
        merge_btn.setCursor(Qt.PointingHandCursor)
        merge_btn.setAutoDefault(False)
        merge_btn.setDefault(True)
        merge_btn.clicked.connect(self._on_merge)
        btn_row.addWidget(merge_btn)
        layout.addLayout(btn_row)

        _wire_overlay_dismiss(overlay, box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

    def _on_replace(self) -> None:
        self._mode = "replace"
        self.accept()

    def _on_merge(self) -> None:
        self._mode = "merge"
        self.accept()

    def import_mode(self) -> str:
        return self._mode if self._mode in ("replace", "merge") else "merge"

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())


class SpecialWearComplexityWarningDialog(QDialog):
    """特殊磨损：产物真实磨损区间过宽时，进入计算页面前的复杂度警告。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = "计算负载过高警告"
        message = (
            "产物磨损范围设置过宽，计算压力将会非常大\n或许应该再把范围缩紧一些"
        )
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close_btn = _build_frameless_modal_content(
            self,
            title,
            message,
            message_object_name="alertDialogMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch(1)
        think_btn = QPushButton("我再想想")
        think_btn.setObjectName("specialWearComplexityThinkAgainBtn")
        think_btn.setCursor(Qt.PointingHandCursor)
        think_btn.setAutoDefault(False)
        think_btn.setDefault(True)
        think_btn.clicked.connect(self.reject)
        btn_row.addWidget(think_btn)
        continue_btn = QPushButton("继续计算")
        continue_btn.setObjectName("specialWearComplexityContinueBtn")
        continue_btn.setCursor(Qt.PointingHandCursor)
        continue_btn.setAutoDefault(False)
        continue_btn.setDefault(False)
        continue_btn.clicked.connect(self.accept)
        btn_row.addWidget(continue_btn)
        layout.addLayout(btn_row)

        _wire_overlay_dismiss(overlay, box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())


class TextPromptDialog(QDialog):
    """单行输入 - 与 ConfirmDialog 同壳，用于替代系统 QInputDialog。"""

    def __init__(
        self,
        title: str,
        label: str,
        parent=None,
        *,
        default: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        overlay = QWidget(self)
        overlay.setObjectName("alertOverlay")
        overlay.setAttribute(Qt.WA_StyledBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(overlay)

        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setAlignment(Qt.AlignCenter)

        box = QFrame()
        box.setObjectName("loginBox")
        box.setFixedWidth(400)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)

        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("loginTitle")
        header.addWidget(title_label)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("loginCloseBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        layout.addLayout(header)
        layout.addSpacing(4)

        form_label = QLabel(label)
        form_label.setObjectName("loginFormLabel")
        layout.addWidget(form_label)

        self._line_edit = QLineEdit()
        self._line_edit.setObjectName("loginInput")
        self._line_edit.setText(default)
        self._line_edit.selectAll()
        self._line_edit.returnPressed.connect(self.accept)
        layout.addWidget(self._line_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("confirmDialogCancelBtn")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("confirmDialogOkBtn")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        overlay_layout.addWidget(box)

        def on_overlay_click(event):
            if event.button() == Qt.LeftButton:
                w = overlay.childAt(event.pos().x(), event.pos().y())
                if w is None or (w != box and not box.isAncestorOf(w)):
                    self.reject()

        overlay.mousePressEvent = on_overlay_click
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())
        # 无框弹窗打开时默认焦点常在「确定」上；延后一帧再聚焦输入框，便于直接键入
        def _focus_input() -> None:
            self._line_edit.setFocus(Qt.FocusReason.PopupFocusReason)
            self._line_edit.selectAll()

        QTimer.singleShot(0, _focus_input)

    def value(self) -> str:
        return self._line_edit.text().strip()


def prompt_text(
    parent,
    title: str,
    label: str,
    *,
    default: str = "",
) -> tuple[str, bool]:
    """显示与主题一致的输入框，返回 (文本, 是否确定)。"""
    dlg = TextPromptDialog(title, label, parent, default=default)
    if dlg.exec() != QDialog.Accepted:
        return "", False
    return dlg.value(), True


class WearInputNoticeDialog(QDialog):
    """炼金第二步「产物磨损设置」进入前：说明磨损输入与程序内数值换算，可选不再提示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = "使用须知"
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        overlay, box, layout, close_btn = _build_frameless_modal_content(
            self,
            title,
            _WEAR_INPUT_NOTICE_TEXT,
            box_width=540,
            box_object_name="wearNoticeBox",
            message_object_name="wearNoticeMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.accept)

        self._dont_show_cb = QCheckBox("不再显示")
        self._dont_show_cb.setObjectName("wearNoticeDontShowCheck")
        self._dont_show_cb.setCursor(Qt.PointingHandCursor)
        self._dont_show_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self._dont_show_cb)

        ok_btn = QPushButton("我知道了")
        ok_btn.setObjectName("alchemySelectFileBtn")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)

        _wire_overlay_dismiss(overlay, box, self, accept=True)

        msg_lbl = self.findChild(QLabel, "wearNoticeMessage")
        if msg_lbl is not None:
            mw = msg_lbl.minimumWidth()
            hfw = msg_lbl.heightForWidth(mw)
            if hfw > 0:
                msg_lbl.setMinimumHeight(hfw)

        self.adjustSize()
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())

    def accept(self) -> None:
        if self._dont_show_cb.isChecked():
            save_alchemy_wear_step2_notice_dismissed(True)
        super().accept()
