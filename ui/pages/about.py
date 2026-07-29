"""About page — product intro and workflow, matching CS2TH-TOOL layout."""

from __future__ import annotations

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
from ui.dialogs.information_dialogs import ABOUT_FLOW_STEPS, show_information_dialog


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
        # 贴顶排布，避免卡片漂在大片留白中间
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
        card_layout.setContentsMargins(24, 20, 24, 18)
        card_layout.setSpacing(12)

        intro = QLabel(
            f"<b>{APP_NAME}</b>，面向 CS2 汰换的本地桌面助手："
            "库存整理、配方计算、磨损模拟与材料采集。"
        )
        intro.setObjectName("aboutBody")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        card_layout.addWidget(intro)

        flow_title = QLabel("操作流程")
        flow_title.setObjectName("aboutFlowTitle")
        card_layout.addWidget(flow_title)

        steps_wrap = QVBoxLayout()
        steps_wrap.setContentsMargins(0, 0, 0, 0)
        steps_wrap.setSpacing(8)
        for index, step in enumerate(ABOUT_FLOW_STEPS, start=1):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            marker = QLabel("①②③④⑤⑥⑦⑧⑨⑩"[index - 1])
            marker.setObjectName("aboutStepMarker")
            marker.setFixedWidth(22)
            text = QLabel(step)
            text.setObjectName("aboutStepText")
            text.setWordWrap(True)
            row.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            steps_wrap.addLayout(row)
        card_layout.addLayout(steps_wrap)

        note = QLabel(
            "计算结果、价格与行情仅供参考，请在实际交易或合成前自行核对。"
            "<br/><b>CS2TH.CN</b> 拥有最终解释权。"
        )
        note.setObjectName("aboutBody")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)
        card_layout.addWidget(note)

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

        # 与内容区同宽，贴标题下方
        inner_layout.addWidget(card)
        inner_layout.addStretch(1)
