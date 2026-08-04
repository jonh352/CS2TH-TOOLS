"""配方表「购买链接 / 操作」列组件与二维码弹窗。"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShowEvent,
)
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

from config import ASSETS_DIR
from ui.asset_cache import cached_asset_pixmap, cached_asset_qicon_svg
from ui.icons import load_svg_icon
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)

# --- 底物一条购买链接（与表格中带链接行一一对应）---------------------------------

_QR_PX = 360
_QR_BOX = 6
_QR_BORDER = 3
_QR_INNER_PAD = 6
_ACTION_ICON_SIZE = 18
_ACTION_BTN_SIZE = 28
_BAN_ICON_PATH = ASSETS_DIR / "ban.svg"
_BAN_ICON_ACTIVE_PATH = ASSETS_DIR / "chosen_ban.svg"
_LOCK_ICON_PATH = ASSETS_DIR / "lock.svg"
_LOCK_ICON_ACTIVE_PATH = ASSETS_DIR / "chosen_lock.svg"


@dataclass(frozen=True)
class QrSlot:
    """弹窗内一条可扫码的底物。"""

    url: str
    logo_path: Path | None
    name: str
    platform: str
    float_value: float | int | str | None = None
    price: float | int | str | None = None


def normalize_purchase_url_key(url: str) -> str:
    return (url or "").strip()


def hint_for_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p == "c5":
        return "请使用相机或C5 APP扫码"
    elif p == "eco":
        return "请使用相机或ECO APP扫码"
    elif p == "yyyp":
        return "请使用相机扫码"
    else: # buff
        return "请使用相机扫码或打开链接后使用BUFF扫码"


def _substrate_name_link_html(url: str, name: str) -> str:
    """皮肤名称：可点击用系统浏览器打开购买页；无 URL 时退化为纯文本。"""
    u = (url or "").strip()
    display = (name or "").strip() or u
    if not display:
        return "—"
    if not u:
        return html.escape(display, quote=False)
    esc = html.escape(display, quote=False)
    href = html.escape(u, quote=True)
    # QLabel 内部 QTextDocument：全局 CSS 比 <a style> / 窗口 QSS 更可靠地去掉链接下划线
    return (
        '<html><head><style type="text/css">'
        "a { text-decoration: none; }"
        "</style></head><body>"
        f'<a href="{href}">{esc}</a>'
        "</body></html>"
    )


def _format_wear_price_line(s: QrSlot) -> str:
    """与配方表底物列一致的磨损 / 价格展示。"""
    fv = s.float_value
    if isinstance(fv, (int, float)):
        fv_s = f"{float(fv):.18f}"
    elif fv is None or fv == "":
        fv_s = "—"
    else:
        fv_s = str(fv)
    pr = s.price
    if isinstance(pr, (int, float)):
        pr_s = f"{float(pr):.2f}"
    elif pr is None or pr == "":
        pr_s = "—"
    else:
        try:
            pr_s = f"{float(pr):.2f}"
        except (TypeError, ValueError):
            pr_s = str(pr)
    price_part = f"￥{pr_s}"
    return f"磨损：{fv_s} | 价格：{price_part}"


# --- 二维码图像 ----------------------------------------------------------------


def _qr_base_pixmap(url: str, px: int) -> QPixmap:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=_QR_BOX,
        border=_QR_BORDER,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    qimg = QImage.fromData(buf.getvalue())
    if qimg.isNull():
        return QPixmap()
    pm = QPixmap.fromImage(qimg)
    return pm.scaled(
        px,
        px,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def compose_qr_pixmap_with_logo(
    url: str,
    logo_path: Path | None,
    *,
    qr_px: int = _QR_PX,
) -> QPixmap:
    base = _qr_base_pixmap(url, qr_px)
    if base.isNull():
        return base
    if not logo_path or not logo_path.is_file():
        return base
    logopm = cached_asset_pixmap(logo_path)
    if logopm.isNull():
        return base
    out = QPixmap(base.size())
    out.fill(Qt.GlobalColor.white)
    p = QPainter(out)
    p.drawPixmap(0, 0, base)
    lw = max(40, int(qr_px * 0.19))
    scaled = logopm.scaled(
        lw,
        lw,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pad = 8
    box_w = scaled.width() + pad * 2
    box_h = scaled.height() + pad * 2
    cx = (out.width() - box_w) // 2
    cy = (out.height() - box_h) // 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(cx, cy, box_w, box_h, 6, 6)
    p.setPen(QPen(QColor("#e2e8f0"), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(cx, cy, box_w, box_h, 6, 6)
    p.drawPixmap(cx + pad, cy + pad, scaled)
    p.end()
    return out


def _center_label(object_name: str) -> QLabel:
    lb = QLabel()
    lb.setObjectName(object_name)
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lb.setWordWrap(True)
    lb.setAutoFillBackground(False)
    return lb


def _nav_button(symbol: str, tip: str) -> QPushButton:
    b = QPushButton(symbol)
    b.setObjectName("purchaseQrNavBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setToolTip(tip)
    return b


def _icon_button(tip: str) -> QPushButton:
    b = QPushButton()
    b.setObjectName("purchaseActionIconBtn")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setToolTip(tip)
    b.setFixedSize(_ACTION_BTN_SIZE, _ACTION_BTN_SIZE)
    b.setIconSize(QSize(_ACTION_ICON_SIZE, _ACTION_ICON_SIZE))
    return b


def _svg_icon(svg_path: Path, *, color: str | None = None, size: int = _ACTION_ICON_SIZE) -> QIcon:
    if not svg_path.is_file():
        return QIcon()
    if color:
        return load_svg_icon(svg_path, color, size=size)
    return cached_asset_qicon_svg(svg_path)


def _wire_purchase_qr_overlay_dismiss(overlay: QWidget, box: QFrame, dialog: QDialog) -> None:
    """点击蒙层空白处关闭（与 ConfirmDialog 一致）。"""

    def on_overlay_click(event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        w = overlay.childAt(event.pos().x(), event.pos().y())
        if w is None or (w is not box and not box.isAncestorOf(w)):
            dialog.reject()

    overlay.mousePressEvent = on_overlay_click  # type: ignore[method-assign]


class PurchaseQrDialog(QDialog):
    """多条 QrSlot 左右切换；展示某条时回调 on_mark_viewed(url_key)。"""

    def __init__(
        self,
        slots: list[QrSlot],
        start_index: int,
        parent: QWidget | None = None,
        *,
        on_mark_viewed: Callable[[str], None] | None = None,
        anchor: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PurchaseQrDialog")
        self.setWindowTitle("查看二维码")
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._slots = slots
        n = len(slots)
        self._idx = max(0, min(start_index, n - 1)) if n else 0
        self._on_mark_viewed = on_mark_viewed
        _ = anchor  # 保留调用方传参；蒙层铺满主窗，不再相对锚点居中

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        overlay = QWidget(self)
        overlay.setObjectName("alertOverlay")
        overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ovl_layout = QVBoxLayout(overlay)
        ovl_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QFrame()
        box.setObjectName("loginBox")
        box.setMinimumWidth(360)
        box.setMaximumWidth(460)
        box.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )

        root = QVBoxLayout(box)
        root.setContentsMargins(24, 20, 24, 22)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.setContentsMargins(0, 0, 0, 0)
        # title_lb = QLabel("查看二维码")
        # title_lb.setObjectName("loginTitle")
        # header.addWidget(title_lb, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("loginCloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)
        root.addSpacing(10)

        self._hint = _center_label("purchaseQrHint")
        root.addWidget(self._hint)
        root.addSpacing(10)

        self._name_lb = _center_label("purchaseQrSubstrate")
        self._name_lb.setTextFormat(Qt.TextFormat.RichText)
        self._name_lb.setOpenExternalLinks(True)
        root.addWidget(self._name_lb)
        root.addSpacing(10)

        self._wear_price_lb = _center_label("purchaseQrSubstrateMeta")
        root.addWidget(self._wear_price_lb)

        inner = QFrame(box)
        inner.setObjectName("purchaseQrInner")
        inner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        inner.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(_QR_INNER_PAD, _QR_INNER_PAD, _QR_INNER_PAD, _QR_INNER_PAD)
        self._img = QLabel(inner)
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setAutoFillBackground(False)
        inner_lay.addWidget(self._img, 0, Qt.AlignmentFlag.AlignCenter)
        root.addWidget(inner, 0, Qt.AlignmentFlag.AlignHCenter)
        self._inner = inner

        self._nav = QWidget(box)
        self._nav.setObjectName("purchaseQrNavRow")
        nav_lay = QHBoxLayout(self._nav)
        nav_lay.setContentsMargins(0, 8, 0, 0)
        nav_lay.setSpacing(10)
        self._prev_btn = _nav_button("◀", "上一条")
        self._prev_btn.clicked.connect(lambda: self._step(-1))
        self._page_lb = QLabel(box)
        self._page_lb.setObjectName("purchaseQrPageLabel")
        self._page_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_lb.setAutoFillBackground(False)
        self._next_btn = _nav_button("▶", "下一条")
        self._next_btn.clicked.connect(lambda: self._step(1))
        nav_lay.addStretch(1)
        nav_lay.addWidget(self._prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        nav_lay.addWidget(self._page_lb, 0, Qt.AlignmentFlag.AlignVCenter)
        nav_lay.addWidget(self._next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        nav_lay.addStretch(1)
        root.addWidget(self._nav)

        ovl_layout.addWidget(box)
        main_layout.addWidget(overlay)

        self._nav.setVisible(n > 1)
        if n <= 1:
            self._prev_btn.setVisible(False)
            self._page_lb.setVisible(False)
            self._next_btn.setVisible(False)
        self._refresh()
        self._emit_viewed()
        _wire_purchase_qr_overlay_dismiss(overlay, box, self)
        install_dialog_topmost_follow_parent(self)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())

    def _emit_viewed(self) -> None:
        if not self._slots or not self._on_mark_viewed:
            return
        self._on_mark_viewed(normalize_purchase_url_key(self._slots[self._idx].url))

    def _step(self, delta: int) -> None:
        j = self._idx + delta
        if 0 <= j < len(self._slots):
            self._idx = j
            self._refresh()
            self._emit_viewed()

    def _refresh(self) -> None:
        if not self._slots:
            return
        s = self._slots[self._idx]
        qr_pm = compose_qr_pixmap_with_logo(s.url, s.logo_path, qr_px=_QR_PX)
        self._img.setPixmap(qr_pm)
        if not qr_pm.isNull():
            self._img.setFixedSize(qr_pm.size())
            self._inner.setFixedSize(
                qr_pm.width() + _QR_INNER_PAD * 2,
                qr_pm.height() + _QR_INNER_PAD * 2,
            )
        self._name_lb.setText(_substrate_name_link_html(s.url, s.name or ""))
        self._wear_price_lb.setText(_format_wear_price_line(s))
        self._hint.setText(hint_for_platform(s.platform))
        n = len(self._slots)
        self._page_lb.setText(f"{self._idx + 1} / {n}")
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < n - 1)


def show_purchase_qr_dialog(
    slots: list[QrSlot],
    start_index: int,
    anchor: QWidget | None,
    on_mark_viewed: Callable[[str], None] | None = None,
) -> None:
    if not slots:
        return
    parent = anchor.window() if anchor is not None else anchor
    PurchaseQrDialog(
        slots,
        start_index,
        parent=parent,
        on_mark_viewed=on_mark_viewed,
        anchor=anchor,
    ).exec()


class PurchaseActionCell(QWidget):
    """表格内操作列：锁定 / 排除图标切换。"""

    def __init__(
        self,
        slot: QrSlot,
        *,
        get_slot_action_state: Callable[[QrSlot], str] | None = None,
        set_slot_action_state: Callable[[QrSlot, str], None] | None = None,
        on_slot_action_changed: Callable[[QrSlot], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("purchaseActionCell")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        self.setMaximumHeight(46)
        self._slot = slot
        self._get_slot_action_state = get_slot_action_state
        self._set_slot_action_state = set_slot_action_state
        self._on_slot_action_changed = on_slot_action_changed

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        lay.addStretch(1)
        self._lock_btn = _icon_button("锁定当前底物")
        self._lock_btn.clicked.connect(lambda: self._toggle_action("locked"))
        self._ban_btn = _icon_button("排除当前底物")
        self._ban_btn.clicked.connect(lambda: self._toggle_action("excluded"))
        lay.addWidget(self._lock_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._ban_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        self._last_visual_key: tuple[str, str] | None = None
        self._theme_refresh_pending = False
        self.refresh_state()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # 折叠配方时可能跳过主题刷新；展开后须按当前调色板重算图标
        self._last_visual_key = None
        self.refresh_state()

    def _state(self) -> str:
        if self._get_slot_action_state is None:
            return "neutral"
        state = self._get_slot_action_state(self._slot)
        if state in {"excluded", "locked"}:
            return state
        return "neutral"

    def _toggle_action(self, target_active: str) -> None:
        if self._set_slot_action_state is None:
            return
        target_state = "neutral" if self._state() == target_active else target_active
        self._set_slot_action_state(self._slot, target_state)
        if self._on_slot_action_changed is not None:
            self._on_slot_action_changed(self._slot)
        self.refresh_state()

    def refresh_state(self) -> None:
        state = self._state()
        normal_color = self.palette().color(QPalette.ColorRole.WindowText).name()
        visual_key = (state, normal_color)
        if visual_key == self._last_visual_key:
            return
        self._last_visual_key = visual_key
        self._apply_button_state(
            self._lock_btn,
            active=state == "locked",
            normal_icon=_LOCK_ICON_PATH,
            active_icon=_LOCK_ICON_ACTIVE_PATH,
            normal_color=normal_color,
            inactive_tip="锁定当前底物",
            active_tip="取消锁定",
        )
        self._apply_button_state(
            self._ban_btn,
            active=state == "excluded",
            normal_icon=_BAN_ICON_PATH,
            active_icon=_BAN_ICON_ACTIVE_PATH,
            normal_color=normal_color,
            inactive_tip="排除当前底物",
            active_tip="取消排除",
        )

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # 与数据采集页/炼金页标题一致：单次深浅切换会连发 Palette + ApplicationPalette；
        # 勿监听 StyleChange（布局/子控件变化时易泛滥）。不可见时跳过，展开时由 showEvent 补刷。
        if event.type() not in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            return
        if not self.isVisible():
            self._last_visual_key = None
            return
        if self._theme_refresh_pending:
            return
        self._theme_refresh_pending = True
        QTimer.singleShot(0, self._run_deferred_theme_refresh)

    def _run_deferred_theme_refresh(self) -> None:
        self._theme_refresh_pending = False
        if not self.isVisible():
            return
        self._last_visual_key = None
        self.refresh_state()

    @staticmethod
    def _apply_button_state(
        btn: QPushButton,
        *,
        active: bool,
        normal_icon: Path,
        active_icon: Path,
        normal_color: str,
        inactive_tip: str,
        active_tip: str,
    ) -> None:
        btn.setIcon(
            _svg_icon(
                active_icon if active else normal_icon,
                color=None if active else normal_color,
            )
        )
        btn.setToolTip(active_tip if active else inactive_tip)
        btn.setProperty("selected", active)
        style = btn.style()
        if style is not None:
            style.unpolish(btn)
            style.polish(btn)


class SubstrateActionColumnHeader(QWidget):
    """锁定/排除批量操作图标（作用于当前配方全部材料）。"""

    def __init__(
        self,
        *,
        on_lock_all: Callable[[], None] | None = None,
        on_exclude_all: Callable[[], None] | None = None,
        show_label: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("substrateActionColumnHeader")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        if show_label:
            self._label = QLabel("操作")
            self._label.setObjectName("substrateActionColumnHeaderLabel")
            self._label.setAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            lay.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addStretch(1)
        else:
            self._label = None
        self._lock_btn = _icon_button("锁定全部底物")
        self._ban_btn = _icon_button("排除全部底物")
        if on_lock_all is not None:
            self._lock_btn.clicked.connect(on_lock_all)
        if on_exclude_all is not None:
            self._ban_btn.clicked.connect(on_exclude_all)
        lay.addWidget(self._lock_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._ban_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._all_locked = False
        self._all_excluded = False
        self._last_visual_key: tuple[bool, bool, str] | None = None
        self._theme_refresh_pending = False
        self.refresh_state(all_locked=False, all_excluded=False)

    def refresh_state(self, *, all_locked: bool, all_excluded: bool) -> None:
        self._all_locked = bool(all_locked)
        self._all_excluded = bool(all_excluded)
        normal_color = self.palette().color(QPalette.ColorRole.WindowText).name()
        visual_key = (self._all_locked, self._all_excluded, normal_color)
        if visual_key == self._last_visual_key:
            return
        self._last_visual_key = visual_key
        PurchaseActionCell._apply_button_state(
            self._lock_btn,
            active=self._all_locked,
            normal_icon=_LOCK_ICON_PATH,
            active_icon=_LOCK_ICON_ACTIVE_PATH,
            normal_color=normal_color,
            inactive_tip="锁定全部底物",
            active_tip="取消全部锁定",
        )
        PurchaseActionCell._apply_button_state(
            self._ban_btn,
            active=self._all_excluded,
            normal_icon=_BAN_ICON_PATH,
            active_icon=_BAN_ICON_ACTIVE_PATH,
            normal_color=normal_color,
            inactive_tip="排除全部底物",
            active_tip="取消全部排除",
        )

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() not in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            return
        if not self.isVisible():
            self._last_visual_key = None
            return
        if self._theme_refresh_pending:
            return
        self._theme_refresh_pending = True
        QTimer.singleShot(0, self._run_deferred_theme_refresh)

    def _run_deferred_theme_refresh(self) -> None:
        self._theme_refresh_pending = False
        if not self.isVisible():
            return
        self._last_visual_key = None
        self.refresh_state(
            all_locked=self._all_locked,
            all_excluded=self._all_excluded,
        )


class PurchaseGoButtonCell(QWidget):
    """表格内「查看」；已查看为灰字样式，仍可再次打开弹窗。"""

    def __init__(
        self,
        url: str,
        *,
        qr_slots: list[QrSlot],
        qr_index: int,
        recipe: dict,
        on_mark_viewed: Callable[[str], None],
        anchor_parent: QWidget | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("purchaseGoCell")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setMaximumHeight(46)
        self._url_key = normalize_purchase_url_key(url)
        self._qr_slots = qr_slots
        self._qr_index = qr_index
        self._recipe = recipe
        self._on_mark_viewed = on_mark_viewed
        self._anchor = anchor_parent

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(0)
        self._btn = QPushButton(self)
        self._btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._btn.setMaximumHeight(24)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._open_dialog)
        lay.addStretch(1)
        lay.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        self.apply_viewed_state()

    def apply_viewed_state(self) -> None:
        pv = self._recipe.get("purchase_viewed")
        viewed = isinstance(pv, dict) and bool(pv.get(self._url_key))
        text = "已查看" if viewed else "查看"
        obj_name = "purchaseGoBtnViewed" if viewed else "purchaseGoBtn"
        self._btn.setText(text)
        self._btn.setObjectName(obj_name)
        # 运行时切换 objectName 时 QSS 不会自动重算，需 polish 才能立刻刷新查看状态样式
        st = self._btn.style()
        if st is not None:
            st.unpolish(self._btn)
            st.polish(self._btn)

    def _open_dialog(self) -> None:
        show_purchase_qr_dialog(
            self._qr_slots,
            self._qr_index,
            self._anchor or self,
            on_mark_viewed=self._on_mark_viewed,
        )
        self.apply_viewed_state()
