"""Settings modal, legal document viewer, and shared copy for About."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config import (
    APP_NAME,
    APP_VERSION,
    CLOSE_BEHAVIOR_EXIT,
    CLOSE_BEHAVIOR_MINIMIZE,
)
from core.close_behavior_prefs import load_close_behavior, save_close_behavior
from core.playwright_channel_prefs import (
    PLAYWRIGHT_CHANNEL_CHROME,
    PLAYWRIGHT_CHANNEL_MSEDGE,
    load_preferred_playwright_channel,
    save_preferred_playwright_channel,
)
from core.storage_util import clear_all_data, clear_cache, storage_usage


def _document(title: str, body: str) -> str:
    return f"""
    <html><head><style>
      body {{ color: palette(text); font-family: "Microsoft YaHei UI";
              line-height: 1.65; margin: 8px 14px; }}
      h2 {{ margin: 0 0 12px; }} h3 {{ margin: 18px 0 6px; }}
      p, li {{ font-size: 13px; }} .muted {{ color: palette(mid); }}
    </style></head><body><h2>{escape(title)}</h2>{body}</body></html>
    """


USER_AGREEMENT = _document(
    "用户协议",
    """
    <p class="muted">更新日期：2026 年 7 月 28 日</p>
    <p>欢迎使用 <b>CS2TH 汰换小助手</b>。本协议由您与 CS2TH / cs2th.cn
    订立。安装、启动、登录或继续使用本软件，即表示您已阅读并同意本协议；
    若不同意，请停止使用本软件。</p>
    <h3>1. 服务内容</h3>
    <p>本软件提供 Steam 库存读取、汰换配方计算与模拟、特殊磨损计算、
    配方管理及第三方交易平台材料检索等辅助功能。计算结果、价格与行情仅供参考，
    不构成交易或投资建议。</p>
    <h3>2. 账号与凭证</h3>
    <p>您应妥善保管 cs2th.cn、Steam、BUFF、悠悠有品等账号及 Cookie、Token。
    您须确保相关账号和凭证属于本人或已获得合法授权，并遵守第三方平台规则。</p>
    <h3>3. 合法使用</h3>
    <p>不得利用本软件实施作弊、攻击、绕过平台限制、批量滥用接口、非法交易、
    逆向破解或其他违法违规行为。第三方平台可能调整接口或风控规则，
    由此导致的暂时不可用不视为本软件违约。</p>
    <h3>4. 风险提示</h3>
    <p>库存、价格、磨损、配方收益及合成结果可能受网络延迟、数据更新、
    平台规则或算法误差影响。进行购买、出售或合成前，请自行复核关键数据。</p>
    <h3>5. 软件与协议更新</h3>
    <p>我们可因功能、安全、运营或法律要求更新软件和协议。重大变更将通过软件、
    官网或其他合理方式提示；更新后继续使用即视为接受变更。</p>
    <h3>6. 责任限制</h3>
    <p>在法律允许范围内，我们不对第三方服务中断、市场波动、用户误操作、
    账号风控或间接损失承担责任。法律另有强制规定的除外。</p>
    """,
)

PRIVACY_POLICY = _document(
    "隐私政策",
    """
    <p class="muted">更新日期：2026 年 7 月 28 日</p>
    <p>我们重视您的隐私。本政策说明 CS2TH 汰换小助手处理信息的方式。</p>
    <h3>1. 本地保存的信息</h3>
    <p>软件会在本机保存界面偏好、Steam 库存缓存、配方、材料采集设置、
    cs2th.cn 登录会话，以及您主动获取的 BUFF Cookie、悠悠 Token。
    第三方登录凭证仅用于您发起的平台登录校验、检索和采集。</p>
    <h3>2. 网络通信</h3>
    <p>当您登录、同步价格、读取官网配方、校验第三方平台登录或采集材料时，
    软件会向 cs2th.cn 或相应第三方平台发送完成操作所必需的请求。
    截图、库存和第三方凭证不会被无目的批量上传。</p>
    <h3>3. 凭证安全</h3>
    <p>Cookie、Token 和会话令牌属于敏感凭证。请勿分享应用数据目录；
    停止使用相关功能时，可退出登录或清除本地数据。软件不会要求您把第三方平台
    密码直接填写到本软件中。</p>
    <h3>4. 第三方服务</h3>
    <p>Steam、BUFF、悠悠有品、C5GAME、ECO 等服务由各自运营者提供，
    其数据处理受各自隐私政策约束。</p>
    <h3>5. 您的选择</h3>
    <p>您可以不登录第三方平台、关闭相关采集来源、删除缓存和本地配方，
    或停止使用并卸载本软件。未成年人应在监护人指导下使用。</p>
    """,
)

ABOUT_FLOW_STEPS: tuple[str, ...] = (
    "在 Steam 库存登录或添加账号，获取库存后筛选饰品并导入炼金计算或炼金模拟",
    "在炼金计算中导入库存或 JSON / JSONL 数据，勾选底物后计算配方",
    "在配方管理中保存与分类结果，并导入炼金模拟查看产物磨损分布",
    "查询特殊磨损后前往材料采集，校验平台登录并抓取候选材料",
)


class LegalDocumentDialog(QDialog):
    """Wide reader for user agreement / privacy policy."""

    def __init__(self, parent=None, *, title: str, html: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} · {title}")
        self.setObjectName("legalDocumentDialog")
        self.resize(760, 620)
        self.setMinimumSize(560, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("settingsModalTitle")
        close_btn = QPushButton("×")
        close_btn.setObjectName("settingsModalClose")
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        head.addWidget(title_label)
        head.addStretch(1)
        head.addWidget(close_btn)
        root.addLayout(head)

        browser = QTextBrowser()
        browser.setObjectName("legalDocumentBrowser")
        browser.setOpenExternalLinks(True)
        browser.setHtml(html)
        root.addWidget(browser, 1)


class _SettingsRadioCard(QFrame):
    def __init__(
        self,
        *,
        title: str,
        detail: str,
        value: str,
        group: QButtonGroup,
        checked: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsRadioCard")
        self.setProperty("checked", checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._value = value
        self.radio = QRadioButton(self)
        self.radio.setObjectName("settingsRadioDot")
        self.radio.setChecked(checked)
        group.addButton(self.radio)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(10)
        layout.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("settingsRadioTitle")
        text_col.addWidget(title_label)
        if detail:
            detail_label = QLabel(detail)
            detail_label.setObjectName("settingsRadioDetail")
            detail_label.setWordWrap(True)
            text_col.addWidget(detail_label)
        layout.addLayout(text_col, 1)

        self.radio.toggled.connect(self._sync_checked_style)
        self._sync_checked_style(checked)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.radio.setChecked(True)
        super().mousePressEvent(event)

    def _sync_checked_style(self, checked: bool) -> None:
        self.setProperty("checked", bool(checked))
        self.style().unpolish(self)
        self.style().polish(self)


class SettingsDialog(QDialog):
    """Compact settings modal aligned with CS2TH-TOOL layout."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setObjectName("settingsDialog")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(440)
        self._backdrop: QWidget | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("settingsModalPanel")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 14)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)
        root.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(0)

        head = QHBoxLayout()
        title = QLabel("设置")
        title.setObjectName("settingsModalTitle")
        close_btn = QPushButton("×")
        close_btn.setObjectName("settingsModalClose")
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close_btn)
        layout.addLayout(head)
        layout.addSpacing(8)

        layout.addWidget(self._section_title("平台登录浏览器"))
        browser_group = QButtonGroup(self)
        current_browser = load_preferred_playwright_channel()
        for text, detail, channel in (
            (
                "Microsoft Edge",
                "外开独立 Edge 登录窗口 · 自动捕获并校验 Cookie / Token",
                PLAYWRIGHT_CHANNEL_MSEDGE,
            ),
            (
                "Google Chrome",
                "外开独立 Chrome 登录窗口 · 自动捕获并校验 Cookie / Token",
                PLAYWRIGHT_CHANNEL_CHROME,
            ),
        ):
            card_row = _SettingsRadioCard(
                title=text,
                detail=detail,
                value=channel,
                group=browser_group,
                checked=current_browser == channel,
                parent=card,
            )
            card_row.radio.toggled.connect(
                lambda checked, value=channel: checked
                and save_preferred_playwright_channel(value)
            )
            layout.addWidget(card_row)
            layout.addSpacing(6)

        layout.addSpacing(8)
        layout.addWidget(self._section_divider())
        layout.addWidget(self._section_title("关闭主窗口时"))
        close_group = QButtonGroup(self)
        current_close = load_close_behavior()
        for text, detail, value in (
            ("最小化到任务栏", "点击关闭按钮时不退出", CLOSE_BEHAVIOR_MINIMIZE),
            ("退出程序", "", CLOSE_BEHAVIOR_EXIT),
        ):
            card_row = _SettingsRadioCard(
                title=text,
                detail=detail,
                value=value,
                group=close_group,
                checked=current_close == value,
                parent=card,
            )
            card_row.radio.toggled.connect(
                lambda checked, behavior=value: checked
                and save_close_behavior(behavior)
            )
            layout.addWidget(card_row)
            layout.addSpacing(6)

        layout.addSpacing(8)
        layout.addWidget(self._section_divider())
        layout.addWidget(self._section_title("存储管理"))
        self.usage_label = QLabel()
        self.usage_label.setObjectName("settingsUsage")
        layout.addWidget(self.usage_label)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        clear_cache_btn = QPushButton("清除缓存")
        clear_cache_btn.setObjectName("alchemySelectFileBtn")
        clear_cache_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_cache_btn.clicked.connect(self._clear_cache)
        clear_all_btn = QPushButton("清除所有数据")
        clear_all_btn.setObjectName("alchemyClearFileBtn")
        clear_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_all_btn.clicked.connect(self._clear_all)
        actions.addWidget(clear_cache_btn, 1)
        actions.addWidget(clear_all_btn, 1)
        layout.addLayout(actions)
        hint = QLabel(
            "清除缓存：价格缓存、头像与采集临时数据。"
            "清除所有数据：登录态、Steam 库存会话、配方等（保留本页设置项）。"
        )
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(10)
        layout.addWidget(self._section_divider())
        layout.addWidget(self._section_title("法律信息"))
        legal_row = QHBoxLayout()
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
        legal_row.addWidget(agreement, 0, Qt.AlignmentFlag.AlignLeft)
        legal_row.addWidget(sep, 0, Qt.AlignmentFlag.AlignLeft)
        legal_row.addWidget(privacy, 0, Qt.AlignmentFlag.AlignLeft)
        legal_row.addStretch(1)
        layout.addLayout(legal_row)

        self._refresh_usage()

    def showEvent(self, event) -> None:
        parent = self.parentWidget()
        if parent is not None and self._backdrop is None:
            self._backdrop = QWidget(parent.window())
            self._backdrop.setObjectName("settingsDialogBackdrop")
            self._backdrop.setGeometry(parent.window().rect())
            self._backdrop.show()
            self._backdrop.raise_()
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def done(self, result: int) -> None:
        if self._backdrop is not None:
            self._backdrop.deleteLater()
            self._backdrop = None
        super().done(result)

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("settingsSectionTitle")
        return label

    @staticmethod
    def _section_divider() -> QFrame:
        line = QFrame()
        line.setObjectName("settingsSectionDivider")
        line.setFixedHeight(1)
        line.setFrameShape(QFrame.Shape.NoFrame)
        return line

    def _refresh_usage(self) -> None:
        info = storage_usage()
        self.usage_label.setText(f"当前已用：{info.get('label') or '—'}")

    def _clear_cache(self) -> None:
        result = clear_cache()
        self._refresh_usage()
        QMessageBox.information(
            self,
            "清除缓存",
            f"已清除缓存。当前已用：{result.get('label') or '—'}",
        )

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除所有数据",
            "将删除本地登录态、Steam 库存会话与配方等数据，且不可恢复。\n"
            "本页设置项会保留。确定继续吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = clear_all_data(keep_settings=True)
        self._refresh_usage()
        QMessageBox.information(
            self,
            "清除所有数据",
            f"已清除本地数据。当前已用：{result.get('label') or '—'}\n"
            "部分变更可能需要重启应用后完全生效。",
        )


def show_information_dialog(parent, page: str) -> None:
    if page == "用户协议":
        LegalDocumentDialog(parent, title="用户协议", html=USER_AGREEMENT).exec()
        return
    if page == "隐私政策":
        LegalDocumentDialog(parent, title="隐私政策", html=PRIVACY_POLICY).exec()
        return
    SettingsDialog(parent).exec()
