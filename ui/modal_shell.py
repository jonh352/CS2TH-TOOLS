"""统一无框小窗壳：蒙层 + 内容框 + 标题行。

所有确认 / 提示 / 表单弹窗应复用此壳，避免 loginBox / 系统 QMessageBox 混用。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# 内容框内边距与间距（全应用一致）
MODAL_BOX_MARGINS = (28, 28, 28, 28)
MODAL_BOX_SPACING = 16
MODAL_WIDTH_SM = 400
MODAL_WIDTH_MD = 480
MODAL_WIDTH_LG = 560


def build_frameless_modal_content(
    dialog: QDialog,
    title: str,
    message: str = "",
    *,
    box_width: int = MODAL_WIDTH_SM,
    box_object_name: str = "loginBox",
    overlay_object_name: str = "alertOverlay",
    message_object_name: str = "alertDialogMessage",
    include_message: bool = True,
) -> tuple[QWidget, QFrame, QVBoxLayout, QPushButton]:
    """蒙层 + 内容框 + 标题行（+ 可选消息）。返回 overlay、box、box 主布局、关闭按钮。"""
    overlay = QWidget(dialog)
    overlay.setObjectName(overlay_object_name)
    overlay.setAttribute(Qt.WA_StyledBackground)

    overlay_layout = QVBoxLayout(overlay)
    overlay_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    box = QFrame()
    box.setObjectName(box_object_name)
    box.setFixedWidth(box_width)

    layout = QVBoxLayout(box)
    layout.setContentsMargins(*MODAL_BOX_MARGINS)
    layout.setSpacing(MODAL_BOX_SPACING)

    header = QHBoxLayout()
    header.setSpacing(12)
    title_label = QLabel(title)
    title_label.setObjectName("loginTitle")
    header.addWidget(title_label, 1)
    close_btn = QPushButton("✕")
    close_btn.setObjectName("loginCloseBtn")
    close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    close_btn.setAutoDefault(False)
    close_btn.setDefault(False)
    header.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(header)

    if include_message and message:
        msg_label = QLabel(message)
        msg_label.setObjectName(message_object_name)
        msg_label.setWordWrap(True)
        msg_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        inner_w = max(
            1,
            box_width
            - layout.contentsMargins().left()
            - layout.contentsMargins().right(),
        )
        msg_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        msg_label.setMinimumWidth(inner_w)
        layout.addWidget(msg_label)

    overlay_layout.addWidget(box)
    return overlay, box, layout, close_btn


def wire_overlay_dismiss(
    overlay: QWidget,
    box: QFrame,
    dialog: QDialog,
    *,
    accept: bool,
) -> None:
    """点击蒙层空白处关闭。"""

    def on_overlay_click(event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            w = overlay.childAt(event.pos().x(), event.pos().y())
            if w is None or (w != box and not box.isAncestorOf(w)):
                (dialog.accept if accept else dialog.reject)()

    overlay.mousePressEvent = on_overlay_click


def add_modal_footer_buttons(
    layout: QVBoxLayout,
    *,
    cancel_text: str = "取消",
    ok_text: str = "确定",
    on_cancel=None,
    on_ok=None,
    cancel_object_name: str = "confirmDialogCancelBtn",
    ok_object_name: str = "confirmDialogOkBtn",
) -> tuple[QPushButton, QPushButton]:
    """标准「取消 + 主操作」底栏。"""
    btn_row = QHBoxLayout()
    btn_row.setSpacing(12)
    btn_row.addStretch(1)
    cancel_btn = QPushButton(cancel_text)
    cancel_btn.setObjectName(cancel_object_name)
    cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel_btn.setAutoDefault(False)
    cancel_btn.setDefault(False)
    if on_cancel is not None:
        cancel_btn.clicked.connect(on_cancel)
    btn_row.addWidget(cancel_btn)
    ok_btn = QPushButton(ok_text)
    ok_btn.setObjectName(ok_object_name)
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.setDefault(True)
    if on_ok is not None:
        ok_btn.clicked.connect(on_ok)
    btn_row.addWidget(ok_btn)
    layout.addLayout(btn_row)
    return cancel_btn, ok_btn
