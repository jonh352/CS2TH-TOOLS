"""皮肤全名搜索框 + 候选列表（行为对齐特殊磨损页，供数据采集等复用）。"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

from PySide6.QtCore import QPoint, QRectF, QSize, QTimer, Qt, QEvent, QObject, Signal
from PySide6.QtGui import QCursor, QFont, QFontMetrics, QPalette, QPainter, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from config import SPECIAL_WEAR_SEARCH_WIDTH
from core.alchemy_quality import get_template_from_goods_name, normalize_name
from core.data_utils import SkinTemplate
from core.special_wear_names import get_skin_full_names_without_appearance
from core.weapon_box_catalog import get_weapon_box_pick_rows

_MATCH_HIGHLIGHT_COLOR = "#ef4444"
_POPUP_MAX_ITEMS = 200
_ROLE_SKIN_SEARCH_PAYLOAD = Qt.ItemDataRole.UserRole + 100
# 候选列表最大高度；内容更少时按行高自适应，避免仅 1 条结果时出现大块空白
_POPUP_MAX_HEIGHT = 320
# 原 QSS ``::item { padding: 6px 8px }`` 的纵向留白；item 上下 padding 已改为 0，由 delegate 绘制皮肤行时补上。
_FETCH_SKIN_CANDIDATE_ITEM_PAD_V = 6
# 武器箱候选行：箱名 ↔ 芯片区、品质 ↔ ST 的水平间距（原 2px 偏紧）
_WEAPON_BOX_NAME_TO_CHIPS_GAP_PX = 10
_WEAPON_BOX_CHIP_TO_CHIP_GAP_PX = 6


def _fetch_skin_list_row_height_px(font: QFont) -> int:
    """无 setItemWidget 的皮肤行须显式 setSizeHint，否则 Qt 在 0 纵向 padding 下会把行高压得过扁。"""
    fm = QFontMetrics(font)
    pv = _FETCH_SKIN_CANDIDATE_ITEM_PAD_V
    return max(28, fm.height() + 2 * pv + 2)


# 与库存页「品质筛选」芯片底色一致（``ui/pages/inventory.py`` ``_INVENTORY_QUALITY_FILTER_CHIPS``）
# Qt 样式表 rgba 的 alpha 须为 0–255 整数，勿用 0.0–1.0 浮点（否则整段无法解析）
_FETCH_CANDIDATE_QUALITY_BG_CN: dict[str, str] = {
    "非凡": "#e4ae39",
    "隐秘": "#eb4b4b",
    "保密": "#d32ce6",
    "受限": "#8847ff",
    "军规级": "#4b69ff",
    "工业级": "#5e98d9",
    "消费级": "#b0c3d9",
}
_FETCH_CANDIDATE_ST_CHIP_BG = "#16a34a"
_FETCH_CANDIDATE_TAG_TEXT = "#ffffff"
# 品质/ST 芯片：上下留白与 chip_h 计算一致，避免字体贴底（原先 padding-y=0 且高度偏紧）
_FETCH_CHIP_BADGE_PAD_V_PX = 2
_FETCH_CHIP_BADGE_PAD_H_PX = 5
# 芯片字号用 QFont 设置（勿在 QSS 里写 font-size，避免与列表字体冲突）；行高不够时自动缩小


def _chip_badge_stylesheet(bg: str) -> str:
    pv = _FETCH_CHIP_BADGE_PAD_V_PX
    ph = _FETCH_CHIP_BADGE_PAD_H_PX
    return (
        f"QLabel {{ background-color: {bg}; font-weight: 600; "
        f"padding: {pv}px {ph}px; border-radius: 3px; color: {_FETCH_CANDIDATE_TAG_TEXT}; "
        "border: none; }"
    )


def _quality_chip_stylesheet(quality_cn: str) -> str:
    bg = _FETCH_CANDIDATE_QUALITY_BG_CN.get((quality_cn or "").strip(), "#64748b")
    return _chip_badge_stylesheet(bg)


def _st_chip_stylesheet() -> str:
    return _chip_badge_stylesheet(_FETCH_CANDIDATE_ST_CHIP_BG)


def _pick_chip_font(list_font: QFont, max_text_height: int) -> QFont:
    """在不超过 ``max_text_height`` 的前提下缩小芯片字体（相对列表字体）。"""
    f = QFont(list_font)
    # 先整体比列表小一档，再按行高压缩
    if f.pointSizeF() > 0.0:
        f.setPointSizeF(max(6.0, f.pointSizeF() - 1.25))
    elif f.pixelSize() > 0:
        f.setPixelSize(max(8, f.pixelSize() - 2))
    cap = max(7, int(max_text_height))
    for _ in range(16):
        h = QFontMetrics(f).height()
        if h <= cap:
            return f
        if f.pointSizeF() > 0.0:
            f.setPointSizeF(max(6.0, f.pointSizeF() - 0.5))
        elif f.pixelSize() > 0:
            f.setPixelSize(max(7, f.pixelSize() - 1))
        else:
            f.setPointSizeF(6.0)
    return f


class _WeaponBoxCandidateRow(QWidget):
    """武器箱候选行：箱名与皮肤候选同字体/配色；品质与 ST 为小号芯片。"""

    def __init__(
        self,
        *,
        field: "SkinSearchField",
        box_name: str,
        quality_cn: str,
        is_st: bool,
        query: str,
        max_name_width: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("fetchWeaponBoxCandidateRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 单层横排 + 布局级 AlignVCenter：避免 VBox+双 stretch 在 QListWidget 子控件里
        # 出现「内容贴底、上方留白」；与纯文本皮肤行视觉一致。
        root_lay = QHBoxLayout(self)
        # 上下与皮肤委托 _FETCH_SKIN_CANDIDATE_ITEM_PAD_V 一致；左右为 0，避免与 QSS
        # ``#fetchSkinCandidateList::item { padding: 0px 8px }`` 叠成双倍缩进。
        root_lay.setContentsMargins(0, _FETCH_SKIN_CANDIDATE_ITEM_PAD_V, 0, _FETCH_SKIN_CANDIDATE_ITEM_PAD_V)
        root_lay.setSpacing(0)
        root_lay.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        list_w = parent if isinstance(parent, QListWidget) else field._candidate_popup
        list_font = QFont(list_w.font())
        fm_list = QFontMetrics(list_font)
        # 单行箱名高度与列表字体一致；垂直居中靠 QLabel 对齐，避免 RichText 在过高盒子里贴底
        name_text_h = fm_list.height()
        name_line_h = name_text_h + 2
        chip_text_cap = max(7, name_line_h - 4)
        chip_font = _pick_chip_font(list_font, chip_text_cap)
        fm_chip = QFontMetrics(chip_font)
        pv = _FETCH_CHIP_BADGE_PAD_V_PX
        # 字高 + 上下 padding + 少许圆角余量，并与箱名行高取 min 以免撑破单行
        chip_h = min(
            name_line_h,
            fm_chip.height() + 2 * pv + 2,
        )
        row_body_h = max(name_line_h, chip_h)

        normal = list_w.palette().color(QPalette.ColorRole.WindowText).name()
        name_lbl = QLabel(self)
        name_lbl.setObjectName("fetchWeaponBoxCandidateName")
        name_lbl.setFont(list_font)
        name_lbl.setAutoFillBackground(False)
        name_lbl.setTextFormat(Qt.TextFormat.RichText)
        name_lbl.setWordWrap(False)
        inner = _match_highlight_html(box_name, query, normal)
        name_lbl.setText(
            f'<div style="background-color: transparent; margin:0; padding:0; '
            f"line-height:{int(row_body_h)}px\">{inner}</div>"
        )
        name_lbl.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        name_lbl.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        name_lbl.setFixedHeight(row_body_h)
        if max_name_width > 0:
            name_lbl.setMaximumWidth(max_name_width)

        chips_wrap = QWidget(self)
        chips_wrap.setObjectName("fetchWeaponBoxCandidateChips")
        chips_wrap.setFixedHeight(row_body_h)
        chips_wrap.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        ch_lay = QHBoxLayout(chips_wrap)
        ch_lay.setContentsMargins(0, 0, 0, 0)
        ch_lay.setSpacing(_WEAPON_BOX_CHIP_TO_CHIP_GAP_PX)
        ch_lay.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )

        q_txt = (quality_cn or "").strip() or "—"
        q_lbl = QLabel(q_txt, chips_wrap)
        q_lbl.setObjectName("fetchWeaponBoxCandidateQualityChip")
        q_lbl.setFont(chip_font)
        q_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        q_lbl.setStyleSheet(_quality_chip_stylesheet(quality_cn))
        q_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        q_lbl.setFixedHeight(chip_h)
        q_lbl.setMinimumWidth(fm_chip.horizontalAdvance(q_txt) + 10)
        ch_lay.addWidget(q_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        if is_st:
            st_lbl = QLabel("ST", chips_wrap)
            st_lbl.setObjectName("fetchWeaponBoxCandidateStChip")
            st_lbl.setFont(chip_font)
            st_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            st_lbl.setStyleSheet(_st_chip_stylesheet())
            st_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            st_lbl.setFixedHeight(chip_h)
            st_lbl.setMinimumWidth(fm_chip.horizontalAdvance("ST") + 10)
            ch_lay.addWidget(st_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        root_lay.addWidget(name_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        root_lay.addSpacing(_WEAPON_BOX_NAME_TO_CHIPS_GAP_PX)
        root_lay.addWidget(chips_wrap, 0, Qt.AlignmentFlag.AlignVCenter)
        root_lay.addStretch(1)

        row_min = root_lay.contentsMargins().top() + root_lay.contentsMargins().bottom() + row_body_h
        self._row_body_min_h = max(28, row_min)
        self.setMinimumHeight(self._row_body_min_h)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

    def sizeHint(self) -> QSize:
        sh = super().sizeHint()
        m = getattr(self, "_row_body_min_h", 28)
        return QSize(sh.width(), max(m, sh.height()))

    def minimumSizeHint(self) -> QSize:
        mh = super().minimumSizeHint()
        m = getattr(self, "_row_body_min_h", 28)
        return QSize(mh.width(), max(m, mh.height()))


def _non_overlapping_match_spans(text: str, query_raw: str) -> list[tuple[int, int]]:
    """``text`` 为已 ``normalize_name`` 的候选串；``query_raw`` 为用户输入，先 normalize 再匹配。"""
    q = normalize_name((query_raw or "").strip())
    if not text or not q:
        return []
    t = text.casefold()
    q = q.casefold()
    if not q:
        return []
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(q)
    while True:
        i = t.find(q, pos)
        if i < 0:
            break
        spans.append((i, i + n))
        pos = i + n
    return spans


def _match_highlight_html(text: str, query: str, normal_color: str) -> str:
    if not text:
        return ""
    spans = _non_overlapping_match_spans(text, query)
    if not spans or not query:
        return f'<span style="color:{normal_color}">{escape(text)}</span>'
    parts: list[str] = []
    last = 0
    for a, b in spans:
        if a > last:
            parts.append(
                f'<span style="color:{normal_color}">{escape(text[last:a])}</span>'
            )
        parts.append(
            f'<span style="color:{_MATCH_HIGHLIGHT_COLOR}">{escape(text[a:b])}</span>'
        )
        last = b
    if last < len(text):
        parts.append(f'<span style="color:{normal_color}">{escape(text[last:])}</span>')
    return "".join(parts)


class _FetchSkinMatchDelegate(QStyledItemDelegate):
    def __init__(self, list_widget: QListWidget, field: "SkinSearchField") -> None:
        super().__init__(list_widget)
        self._field = field
        self._list_widget = list_widget

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if not (index.flags() & Qt.ItemFlag.ItemIsSelectable):
            super().paint(painter, option, index)
            return
        it = self._list_widget.itemFromIndex(index)
        if it is not None and self._list_widget.itemWidget(it) is not None:
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            widget = opt.widget
            style = widget.style() if widget else QApplication.style()
            style.drawPrimitive(
                QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget
            )
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()

        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget
        )

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str):
            text = str(text or "")
        query = getattr(self._field, "_highlight_query", "") or ""

        if opt.state & QStyle.StateFlag.State_Selected:
            normal = opt.palette.color(QPalette.ColorRole.HighlightedText).name()
        else:
            normal = opt.palette.color(QPalette.ColorRole.Text).name()

        html = _match_highlight_html(text, query, normal)
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setDefaultFont(opt.font)
        doc.setHtml(f'<span style="white-space:pre-wrap">{html}</span>')

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget
        )
        pv = _FETCH_SKIN_CANDIDATE_ITEM_PAD_V
        if text_rect.isValid() and text_rect.height() > pv * 2 + 2:
            text_rect = text_rect.adjusted(0, pv, 0, -pv)
        tw = max(1, text_rect.width() if text_rect.width() > 0 else opt.rect.width() - 8)
        painter.save()
        painter.translate(text_rect.topLeft())
        doc.setTextWidth(tw)
        clip = QRectF(0, 0, tw, text_rect.height())
        painter.setClipRect(clip.toRect())
        doc.drawContents(painter, clip)
        painter.restore()


class SkinSearchField(QWidget):
    """单行搜索 + 下拉候选；添加条目时须 ``selected_from_dropdown()`` 非空（须点选列表项）。

    ``include_weapon_box_search`` 为假时不加载武器箱候选、不占武器箱首项回车逻辑（如炼金模拟单槽选皮）。
    ``auto_pick_first_on_focus_out`` 为真时失焦行为与回车一致：有候选则填首项，无则清空。
    """

    skin_resolved = Signal(object)  # ``SkinTemplate | None``，点选或失去有效选择时发出
    weapon_box_guns_selected = Signal(list)  # 点选武器箱+品质行时：标准化全名列表

    def __init__(
        self,
        parent=None,
        *,
        min_width: int | None = None,
        allowed_qualities: frozenset[str] | None = None,
        excluded_qualities: frozenset[str] | None = None,
        template_extra_predicate: Callable[[SkinTemplate], bool] | None = None,
        candidate_popup_above: bool = False,
        candidate_list_object_name: str = "fetchSkinCandidateList",
        line_edit_object_name: str = "fetchSkinSearchEdit",
        include_weapon_box_search: bool = True,
        auto_pick_first_on_focus_out: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("fetchSkinSearchBlock")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._all_names: list[str] = []
        self._names_loaded = False
        self._weapon_box_rows: list[tuple[str, str, bool, tuple[str, ...]]] = []
        self._highlight_query: str = ""
        self._selected_full_name: str | None = None
        self._allowed_qualities: frozenset[str] | None = allowed_qualities
        self._excluded_qualities: frozenset[str] | None = excluded_qualities
        self._template_extra_predicate: Callable[[SkinTemplate], bool] | None = (
            template_extra_predicate
        )
        self._candidate_popup_above: bool = candidate_popup_above
        self._include_weapon_box_search = bool(include_weapon_box_search)
        self._auto_pick_first_on_focus_out = bool(auto_pick_first_on_focus_out)
        mw = int(min_width) if min_width is not None else SPECIAL_WEAR_SEARCH_WIDTH
        self._line_min_width = max(120, mw)
        self._popup_text_width = max(40, self._line_min_width - 24)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_filter)
        self._last_skin_emit_key: object | None = object()  # 哨兵，避免与 paint_index 撞车
        self._focus_out_debounce = QTimer(self)
        self._focus_out_debounce.setSingleShot(True)
        self._focus_out_debounce.setInterval(0)
        self._focus_out_debounce.timeout.connect(self._on_search_focus_out_deferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.line_edit = QLineEdit(self)
        self.line_edit.setObjectName(line_edit_object_name)
        self.line_edit.setPlaceholderText(
            "输入皮肤或武器箱名称搜索…"
            if self._include_weapon_box_search
            else "输入皮肤名称搜索…"
        )
        self.line_edit.setClearButtonEnabled(True)
        self.line_edit.setMinimumWidth(self._line_min_width)
        self.line_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.line_edit.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.line_edit.textChanged.connect(self._on_search_text_changed)
        self.line_edit.returnPressed.connect(self._on_search_return_pressed)
        lay.addWidget(self.line_edit)

        self._candidate_popup = QListWidget(self)
        self._candidate_popup.setObjectName(candidate_list_object_name)
        self._candidate_popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._candidate_popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._candidate_popup.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._candidate_popup.setUniformItemSizes(False)
        self._candidate_popup.setSpacing(2)
        self._candidate_popup.itemClicked.connect(self._on_candidate_selected)
        self._candidate_popup.itemActivated.connect(self._on_candidate_selected)
        self._candidate_popup.setItemDelegate(
            _FetchSkinMatchDelegate(self._candidate_popup, self)
        )
        self._candidate_popup.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating, True
        )
        self._candidate_popup.hide()

        self.line_edit.installEventFilter(self)

    def refresh_clear_button(self) -> None:
        """重绑清除按钮：窄卡网格重排 / 显隐切换后 Qt 有时不刷新 × 区域。"""
        le = self.line_edit
        if not le.isClearButtonEnabled():
            return
        le.setClearButtonEnabled(False)
        le.setClearButtonEnabled(True)
        le.update()

    @staticmethod
    def _scroll_line_edit_to_logical_start(le: QLineEdit) -> None:
        """将光标置于开头并取消选择，使可视区域滚到最左侧（失焦/回车/点选后也会调用）。"""
        le.setCursorPosition(0)
        le.deselect()

    def _defer_scroll_line_to_start(self) -> None:
        def _go() -> None:
            if not self.isVisible():
                return
            self._scroll_line_edit_to_logical_start(self.line_edit)

        QTimer.singleShot(0, _go)

    def _emit_skin_resolved(self, tpl: object | None) -> None:
        key = None if tpl is None else getattr(tpl, "paint_index", None)
        if key == self._last_skin_emit_key:
            return
        self._last_skin_emit_key = key
        self.skin_resolved.emit(tpl)

    def line_edit_widget(self) -> QLineEdit:
        return self.line_edit

    def _template_matches_filters(self, t: SkinTemplate | None) -> bool:
        if t is None:
            return False
        if self._allowed_qualities is not None and t.quality not in self._allowed_qualities:
            return False
        if self._excluded_qualities is not None and t.quality in self._excluded_qualities:
            return False
        if self._template_extra_predicate is not None and not self._template_extra_predicate(t):
            return False
        return True

    def _invalidate_selection_if_filters_fail(self) -> None:
        name = (self._selected_full_name or "").strip()
        if not name:
            return
        t = get_template_from_goods_name(name)
        if not self._template_matches_filters(t):
            self.clear_for_next_entry()

    def set_allowed_qualities(self, allowed: frozenset[str] | None) -> None:
        """``None`` 表示不限制品质；否则仅保留模板 ``quality`` 落在集合内的候选。"""
        self._allowed_qualities = allowed
        self._invalidate_selection_if_filters_fail()
        self._filter_timer.start()

    def set_excluded_qualities(self, excluded: frozenset[str] | None) -> None:
        """``None`` 表示不排除；否则 ``quality`` 落在集合内的模板不出现在候选中。"""
        self._excluded_qualities = excluded
        self._invalidate_selection_if_filters_fail()
        self._filter_timer.start()

    def set_template_extra_predicate(
        self, pred: Callable[[SkinTemplate], bool] | None
    ) -> None:
        """额外条件；为 ``None`` 时不校验。用于五合一排除 ★ 刀具等。"""
        self._template_extra_predicate = pred
        self._invalidate_selection_if_filters_fail()
        self._filter_timer.start()

    def set_candidate_popup_above(self, above: bool) -> None:
        self._candidate_popup_above = bool(above)
        if self._candidate_popup.isVisible():
            self._position_candidate_popup()

    def ensure_names_loaded(self) -> None:
        if self._names_loaded:
            return
        self._all_names = get_skin_full_names_without_appearance()
        self._weapon_box_rows = (
            get_weapon_box_pick_rows() if self._include_weapon_box_search else []
        )
        self._names_loaded = True

    def _popup_host(self) -> QWidget:
        """候选列表挂在顶层窗口上，避免被同一行右侧按钮遮挡（特殊磨损页无此问题）。"""
        """候选列表挂在 content_area（objectName=contentArea）上。
        主题把 pages_alchemy / page_special_wear 等 QSS 只设在 content_area 子树上；若挂在
        QMainWindow 根上则 #fetchSkinCandidateList / #alchemySimulationSkinCandidateList 等规则不生效，弹出框会退回原生样式。
        content_area 与主窗口客户区同大，raise_() 后仍可盖住同行右侧按钮。
        """
        w: QWidget | None = self
        while w is not None:
            if w.objectName() == "contentArea":
                return w
            w = w.parentWidget()
        win = self.window()
        return win if win is not None else self

    def _reparent_popup_to_host(self) -> None:
        host = self._popup_host()
        if self._candidate_popup.parent() is host:
            return
        self._candidate_popup.setParent(host)

    def selected_from_dropdown(self) -> str:
        """从列表点选确认后非空；回车键在有候选时可自动取首项（见 ``_enforce_confirmed_skin_or_clear``）。"""
        return (self._selected_full_name or "").strip()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.line_edit and event.type() == QEvent.Type.FocusOut:
            self._focus_out_debounce.start()
        return super().eventFilter(watched, event)

    def _commit_picked_full_name(self, full_name: str) -> None:
        """写入规范全名、同步模板与滚动（列表点选 / 回车首项确认共用）。"""
        text = (full_name or "").strip()
        if not text:
            return
        self._selected_full_name = text
        self.line_edit.blockSignals(True)
        try:
            self.line_edit.setText(text)
        finally:
            self.line_edit.blockSignals(False)
        self._highlight_query = text
        tpl = get_template_from_goods_name(text)
        self._emit_skin_resolved(tpl)
        self._defer_scroll_line_to_start()

    def _current_filter_subset(
        self,
    ) -> list[tuple[tuple, tuple[str, str, bool] | None]]:
        """``(payload, weapon_box_meta|None)``；payload 为 ``("skin", 全名)`` 或 ``("weapon_box", 全名元组)``。"""
        self.ensure_names_loaded()
        q_raw = self.line_edit.text().strip()
        q_norm = normalize_name(q_raw)
        if not q_norm:
            return []
        q_fold = q_norm.casefold()
        weapon_hits: list[tuple[tuple, tuple[str, str, bool] | None]] = []
        if self._include_weapon_box_search:
            for box_name, q_cn, is_st, names in self._weapon_box_rows:
                if not box_name.strip():
                    continue
                hay = f"{normalize_name(box_name)} {q_cn}".strip()
                if is_st:
                    # 勿拼入「StatTrak」：子串匹配会令 r/t/a/s/k 等字母命中全部 ST 行
                    hay += " ST 暗金"
                if q_fold in hay.casefold():
                    weapon_hits.append(
                        (("weapon_box", names), (box_name, q_cn, is_st)),
                    )
        skin_hits: list[tuple[tuple, tuple[str, str, bool] | None]] = []
        if self._all_names:
            for n in self._all_names:
                if q_fold not in n.casefold():
                    continue
                if not self._template_matches_filters(get_template_from_goods_name(n)):
                    continue
                skin_hits.append((("skin", n), None))
        merged = weapon_hits + skin_hits
        return merged[:_POPUP_MAX_ITEMS]

    def _enforce_confirmed_skin_or_clear(self) -> None:
        """回车：当前输入下若有候选则填第一项，无则清空。"""
        self._hide_candidate_popup()
        line = self.line_edit.text().strip()
        if not line:
            return
        sel = (self._selected_full_name or "").strip()
        if sel == line:
            return
        subset = self._current_filter_subset()
        if subset:
            payload0, _wb0 = subset[0]
            if (
                isinstance(payload0, tuple)
                and len(payload0) == 2
                and payload0[0] == "weapon_box"
            ):
                self._hide_candidate_popup()
                self._selected_full_name = None
                self._emit_skin_resolved(None)
                return
            self._commit_picked_full_name(subset[0][0][1])
        else:
            self.clear_for_next_entry()

    def _finalize_on_search_focus_out(self) -> None:
        """失焦：关闭候选；默认清空未确认输入，``auto_pick_first_on_focus_out`` 时采纳首项。"""
        if self._auto_pick_first_on_focus_out:
            self._enforce_confirmed_skin_or_clear()
            return
        self._hide_candidate_popup()
        line = self.line_edit.text().strip()
        if not line:
            return
        sel = (self._selected_full_name or "").strip()
        if sel == line:
            return
        self.clear_for_next_entry()

    def _on_search_return_pressed(self) -> None:
        if not self.isVisible():
            return
        self._enforce_confirmed_skin_or_clear()
        self._defer_scroll_line_to_start()

    def _on_search_focus_out_deferred(self) -> None:
        if not self.isVisible():
            return
        nw = QApplication.focusWidget()
        if nw is self.line_edit:
            return
        if nw is not None and (
            nw is self._candidate_popup or self._candidate_popup.isAncestorOf(nw)
        ):
            return
        # 候选框使用 NoFocus，点滚动条时 focusWidget 常不是 popup 子控件；
        # 需按鼠标命中补判，避免将「滚动候选列表」误判为外部点击。
        hovered = QApplication.widgetAt(QCursor.pos())
        if hovered is not None and (
            hovered is self._candidate_popup
            or self._candidate_popup.isAncestorOf(hovered)
        ):
            return
        self._finalize_on_search_focus_out()
        self._defer_scroll_line_to_start()

    def _hide_candidate_popup(self) -> None:
        self._candidate_popup.hide()

    def _compute_popup_list_height(self) -> int:
        """按行累计高度（含多行委托），无再强行垫高最小空白。"""
        lw = self._candidate_popup
        n = lw.count()
        if n <= 0:
            return 0
        fm = lw.fontMetrics()
        fallback_row = max(fm.height() + 12, 28)
        body = 0
        for i in range(n):
            rh = lw.sizeHintForRow(i)
            if rh <= 0:
                rh = fallback_row
            body += rh
        if n > 1:
            body += lw.spacing() * (n - 1)
        if body <= 0:
            body = n * fallback_row
        frame = lw.frameWidth() * 2
        # QSS：列表 padding 约 4px；与 item padding 叠加留一点余量
        pad = 10
        return frame + body + pad

    def _position_candidate_popup(self) -> None:
        self._reparent_popup_to_host()
        host = self._candidate_popup.parent()
        if host is None:
            return
        w = max(1, self.line_edit.width())
        self._popup_text_width = max(40, w - 24)
        fm = self.line_edit.fontMetrics()
        ed_h = max(
            self.line_edit.height(),
            self.line_edit.sizeHint().height(),
            fm.height() + 12,
            28,
        )
        self._candidate_popup.setFixedWidth(w)
        lw = self._candidate_popup
        lw.updateGeometry()
        content_h = self._compute_popup_list_height()
        if content_h <= 0:
            content_h = max(24, lw.sizeHint().height())
        max_h = _POPUP_MAX_HEIGHT
        if content_h > max_h:
            h = max_h
            lw.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        else:
            h = max(24, content_h)
            lw.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._candidate_popup.setFixedHeight(h)
        edit_top_left_global = self.line_edit.mapToGlobal(QPoint(0, 0))
        top_left_in_host = host.mapFromGlobal(edit_top_left_global)
        if self._candidate_popup_above:
            gap = 2
            y = top_left_in_host.y() - h - gap
            self._candidate_popup.move(top_left_in_host.x(), max(0, y))
        else:
            below_global = self.line_edit.mapToGlobal(QPoint(0, ed_h))
            below_in_host = host.mapFromGlobal(below_global)
            self._candidate_popup.move(below_in_host)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._candidate_popup.isVisible():
            self._position_candidate_popup()

    def hideEvent(self, event) -> None:
        self._hide_candidate_popup()
        super().hideEvent(event)

    def _on_search_text_changed(self, text: str) -> None:
        had_selection = self._selected_full_name is not None
        if (self._selected_full_name or "").strip() != text.strip():
            self._selected_full_name = None
            if had_selection:
                self._emit_skin_resolved(None)
        self._filter_timer.start()

    def _apply_filter(self) -> None:
        self.ensure_names_loaded()
        # 筛选规则或文本被程序化更新时会启动 _filter_timer；无焦点时不应弹出候选（如炼金模拟切换五/十合一）。
        if not self.line_edit.hasFocus():
            self._hide_candidate_popup()
            return
        q_raw = self.line_edit.text().strip()
        self._highlight_query = q_raw
        self._candidate_popup.clear()

        subset = self._current_filter_subset()
        if not subset:
            self._hide_candidate_popup()
            return

        w_line = max(1, self.line_edit.width())
        self._popup_text_width = max(40, w_line - 24)
        # 为武器箱行右侧芯片区 + 加大后的水平间距预留宽度，避免箱名区过窄
        chip_reserve = 152
        name_max = max(60, self._popup_text_width - chip_reserve)

        for payload, wb_meta in subset:
            it = QListWidgetItem()
            it.setData(_ROLE_SKIN_SEARCH_PAYLOAD, payload)
            if wb_meta is not None:
                box_name, q_cn, is_st = wb_meta
                row = _WeaponBoxCandidateRow(
                    field=self,
                    box_name=box_name,
                    quality_cn=q_cn,
                    is_st=is_st,
                    query=q_raw,
                    max_name_width=name_max,
                    parent=self._candidate_popup,
                )
                it.setText(box_name)
                self._candidate_popup.addItem(it)
                self._candidate_popup.setItemWidget(it, row)
                row.updateGeometry()
                row_floor = int(getattr(row, "_row_body_min_h", 28))
                row_h = max(row_floor, row.sizeHint().height(), row.minimumSizeHint().height())
                it.setSizeHint(QSize(w_line, row_h))
            else:
                skin_name = payload[1]
                it.setText(skin_name)
                skin_h = _fetch_skin_list_row_height_px(self._candidate_popup.font())
                it.setSizeHint(QSize(w_line, skin_h))
                self._candidate_popup.addItem(it)
        self._candidate_popup.updateGeometry()
        self._position_candidate_popup()
        self._candidate_popup.show()
        self._candidate_popup.raise_()

    def _on_candidate_selected(self, item: QListWidgetItem) -> None:
        if not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            return
        text = item.text().strip()
        if not text:
            return
        self._hide_candidate_popup()
        payload = item.data(_ROLE_SKIN_SEARCH_PAYLOAD)
        if isinstance(payload, tuple) and len(payload) == 2:
            kind, data = payload
            if kind == "weapon_box" and isinstance(data, (tuple, list)):
                names = [str(x).strip() for x in data if str(x).strip()]
                if names:
                    self.weapon_box_guns_selected.emit(names)
                self.clear_for_next_entry()
                return
        self._commit_picked_full_name(text)

    def restore_picked_skin(self, full_name: str) -> None:
        """从快照恢复：假定此前已 ``clear_for_next_entry``，用于与切换模式前的状态无关的独立槽位。"""
        text = (full_name or "").strip()
        if not text:
            return
        self._commit_picked_full_name(text)

    def revalidate_current_selection(self) -> None:
        """当前筛选规则变化后校验已选是否仍合法，不合法则清空。"""
        self._invalidate_selection_if_filters_fail()

    def clear_for_next_entry(self) -> None:
        self._selected_full_name = None
        self.line_edit.clear()
        self._emit_skin_resolved(None)
