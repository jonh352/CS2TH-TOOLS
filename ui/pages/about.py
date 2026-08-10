"""About page — product intro and workflow, matching CS2TH-TOOL layout."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME, APP_VERSION, CONTENT_PAGE_LAYOUT_MARGINS
from ui.components import PageHeader
from ui.dialogs.information_dialogs import (
    ABOUT_FLOW_STEPS,
    ABOUT_FLOW_TIPS,
    show_information_dialog,
)

_STEP_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"


class AboutPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aboutPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        root.setSpacing(12)
        root.addWidget(PageHeader("ABOUT", "关于"))

        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll, 1)

        inner = QWidget()
        inner.setObjectName("aboutScrollInner")
        scroll.setWidget(inner)
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 4, 0, 12)
        inner_layout.setSpacing(0)
        inner_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        card = QFrame()
        card.setObjectName("aboutCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 18, 22, 16)
        card_layout.setSpacing(14)

        intro = QLabel(
            f"<b>{escape(APP_NAME)}</b> 是面向 CS2 汰换的本地桌面助手。"
            "可整理 Steam 库存、算配方、模拟产物磨损，"
            "并从 BUFF / 悠悠有品 / C5GAME / ECO 按磨损区间采集可买材料。"
            "未登录或无会员权限时仍可浏览各页面，登录并具备汰换会员/大会员或公测权限后可使用功能。"
        )
        intro.setObjectName("aboutBody")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        card_layout.addWidget(intro)

        flow_title = QLabel("操作说明（按页面）")
        flow_title.setObjectName("aboutFlowTitle")
        card_layout.addWidget(flow_title)

        steps_wrap = QVBoxLayout()
        steps_wrap.setContentsMargins(0, 0, 0, 0)
        steps_wrap.setSpacing(10)
        for index, step in enumerate(ABOUT_FLOW_STEPS, start=1):
            steps_wrap.addWidget(self._build_step_block(index, step))
        card_layout.addLayout(steps_wrap)

        tips_title = QLabel("使用提醒")
        tips_title.setObjectName("aboutFlowTitle")
        card_layout.addWidget(tips_title)

        tips_wrap = QVBoxLayout()
        tips_wrap.setContentsMargins(2, 0, 0, 0)
        tips_wrap.setSpacing(6)
        for tip in ABOUT_FLOW_TIPS:
            tip_label = QLabel(f"· {escape(tip)}")
            tip_label.setObjectName("aboutTipItem")
            tip_label.setWordWrap(True)
            tip_label.setTextFormat(Qt.TextFormat.RichText)
            tips_wrap.addWidget(tip_label)
        card_layout.addLayout(tips_wrap)

        legal_row = QHBoxLayout()
        legal_row.setContentsMargins(0, 2, 0, 0)
        legal_row.setSpacing(0)
        agreement = QPushButton("《用户协议》")
        agreement.setObjectName("settingsLegalLink")
        agreement.setCursor(Qt.CursorShape.PointingHandCursor)
        agreement.clicked.connect(
            lambda: show_information_dialog(self, "用户协议")
        )
        privacy = QPushButton("《隐私政策》")
        privacy.setObjectName("settingsLegalLink")
        privacy.setCursor(Qt.CursorShape.PointingHandCursor)
        privacy.clicked.connect(
            lambda: show_information_dialog(self, "隐私政策")
        )
        sep = QLabel(" · ")
        sep.setObjectName("settingsLegalSep")
        legal_row.addWidget(agreement)
        legal_row.addWidget(sep)
        legal_row.addWidget(privacy)
        legal_row.addStretch(1)
        card_layout.addLayout(legal_row)

        version = QLabel(f"开发版 · v{APP_VERSION}")
        version.setObjectName("aboutFootNote")
        card_layout.addWidget(version)

        inner_layout.addWidget(card)
        inner_layout.addStretch(1)

    def _build_step_block(self, index: int, step: dict) -> QFrame:
        block = QFrame()
        block.setObjectName("aboutStepBlock")
        block.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        marker = QLabel(
            _STEP_MARKERS[index - 1] if index <= len(_STEP_MARKERS) else f"{index}."
        )
        marker.setObjectName("aboutStepMarker")
        marker.setFixedWidth(22)
        title = QLabel(str(step.get("title") or ""))
        title.setObjectName("aboutStepTitle")
        title.setWordWrap(True)
        head.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(title, 1)
        layout.addLayout(head)

        summary = str(step.get("summary") or "").strip()
        if summary:
            summary_label = QLabel(summary)
            summary_label.setObjectName("aboutStepSummary")
            summary_label.setWordWrap(True)
            layout.addWidget(summary_label)

        groups = step.get("groups") or ()
        for group_title, items in groups:
            group_wrap = QVBoxLayout()
            group_wrap.setContentsMargins(30, 0, 0, 0)
            group_wrap.setSpacing(4)
            heading = str(group_title or "").strip()
            if heading:
                group_label = QLabel(heading)
                group_label.setObjectName("aboutGroupTitle")
                group_wrap.addWidget(group_label)
            for item in items:
                bullet = QLabel(f"· {escape(str(item))}")
                bullet.setObjectName("aboutBullet")
                bullet.setWordWrap(True)
                bullet.setTextFormat(Qt.TextFormat.RichText)
                group_wrap.addWidget(bullet)
            layout.addLayout(group_wrap)

        return block
