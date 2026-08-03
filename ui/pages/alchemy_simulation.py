"""炼金模拟页：五合一 / 十合一底物卡片，校验后进入计算（计算逻辑待接）。"""

from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import QEvent, QLocale, QObject, Qt, QTimer
from PySide6.QtGui import (
    QDoubleValidator,
    QFont,
    QImage,
    QImageReader,
    QKeyEvent,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import (
    ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH,
    ALCHEMY_SIMULATION_ICON_PATH,
    ALCHEMY_SIMULATION_SKIN_SEARCH_MIN_WIDTH,
    CONTENT_PAGE_LAYOUT_MARGINS,
)
from core.alchemy_calc import (
    apply_simulation_prices_and_recipe_metrics,
    compute_tradeup_simulation_products,
    format_inventory_yuan_price,
)
from core.alchemy_quality import resolve_inventory_skin_template
from core.saved_recipes import (
    default_save_recipe_dialog_title,
    format_recipe_summary_line,
    save_recipe_file,
)
from ui.app_settings import save_last_recipe_save_folder_id
from ui.dialogs.move_recipe_folder_dialog import (
    MoveRecipeFolderDialog,
    build_all_recipe_folder_pick_targets,
)
from core.data_utils import (
    APPEARANCE,
    APPEARANCE_MAP,
    INVENTORY_WEAR_BADGE_TEXT_COLOR,
    SkinInstance,
    SkinTemplate,
    WEAR_ZONE_COLORS,
    is_star_knife_skin_template,
    tradeup_display_quality,
    wear_zone_index,
)
from core.inventory_icons import weapon_image_path_from_skin_template
from ui.feedback import show_alert
from ui.icons import load_svg_icon
from ui.widgets.float_line_edit import format_float_shortest
from ui.widgets.segmented_switch import SegmentedCheckSwitch
from ui.widgets.skin_search_field import SkinSearchField
from ui.widgets.toast import show_toast
from ui.widgets.wear_scale_legend import WearScaleLegendWidget
from ui.workers.alchemy_workers import FetchPriceWorker
from ui.weapon_card_image_area import WeaponCardImageArea, line_and_tint_for_quality_cn

_SIM_MAX_CARDS = 10
# 与库存页枪图槽一致 220×140；图区高 = 顶边距 8 + 图高 + 品质底条 3（见 weapon_card_image_area._IMAGE_MARGINS）
_SIM_WEAPON_ICON_W_PX = 154
_SIM_WEAPON_ICON_H_PX = 98
_SIM_IMAGE_AREA_H = 8 + _SIM_WEAPON_ICON_H_PX + 3
# 底物卡 246、产物卡 262（原 274，合并名称/磨损/价格后 -12）与图区高度联动时自行同步
_SIM_CARD_FIXED_HEIGHT = 246
# 名称+磨损+价格并入同一 meta 布局后，根 VBox 少 2 段 spacing(6)，高度减 12
_SIM_RESULT_CARD_HEIGHT = 260
_SIM_GRID_SPACING = 12
_SIM_WEAR_MAX_DECIMALS = 18
_SIM_SUBSTRATE_INVALID_MESSAGE = "请为每个底物槽选择皮肤，并填写磨损"

# 与需求方提供的品质色一致（非凡未给出，沿用游戏向金色）
_SIM_QUALITY_BG = {
    "消费级": "#b0c3d9",
    "工业级": "#5e98d9",
    "军规级": "#4b69ff",
    "受限": "#8847ff",
    "保密": "#d32ce6",
    "隐秘": "#eb4b4b",
    "非凡": "rgba(228, 174, 57, 1)",
}
_SIM_QUALITY_TEXT_COLOR = {
    "消费级": "#ffffff",
    "工业级": "#ffffff",
    "军规级": "#ffffff",
    "受限": "#ffffff",
    "保密": "#ffffff",
    "隐秘": "#ffffff",
    "非凡": "#ffffff",
}

_COVERT_ONLY: frozenset[str] = frozenset({"隐秘"})
# 十合一底物不含「隐秘」「非凡」（与五合一仅隐秘区分）
_TEN_EXCLUDED_QUALITIES: frozenset[str] = frozenset({"隐秘", "非凡"})


def _five_mode_search_predicate(t: SkinTemplate) -> bool:
    """五合一仅允许枪类等的「隐秘」；★ 刀具在 meta 中暗金常为隐秘，仍不可作此类底物。"""
    return not is_star_knife_skin_template(t)


def _load_local_image_pixmap(path: str) -> QPixmap | None:
    reader = QImageReader(path)
    if hasattr(reader, "setAutoDetectImageFormat"):
        reader.setAutoDetectImageFormat(True)
    img = reader.read()
    if not img.isNull():
        pix = QPixmap.fromImage(img)
        if not pix.isNull():
            return pix
    pix = QPixmap(path)
    if not pix.isNull():
        return pix
    fallback = QImage(path)
    if not fallback.isNull():
        pix = QPixmap.fromImage(fallback)
        if not pix.isNull():
            return pix
    return None


def _parse_wear_value(text: str) -> float | None:
    s = (text or "").strip().replace("。", ".")
    if not s or s == ".":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _wear_in_template_range(tpl: SkinTemplate, wear: float) -> bool:
    lo, hi = float(tpl.min_float), float(tpl.max_float)
    return lo - 1e-9 <= wear <= hi + 1e-9


def _skin_tradeup_display_name_with_appearance(tpl: SkinTemplate, float_val: float) -> str:
    """与炼金产物命名一致：武器 | 皮肤 + 中文磨损区间（如 久经沙场）。"""
    base = f"{tpl.weapon_name} | {tpl.skin_name}" if tpl.skin_name else tpl.weapon_name
    app = SkinInstance.get_appearance(float(float_val))
    if not app:
        return base
    return f"{base} ({app})"


def _format_sim_price_prefix_yuan(value: float | None) -> str:
    v = value if value is not None and value > 0 else None
    return f"价格：{format_inventory_yuan_price(v)}"


def _format_sim_prob(p: float) -> str:
    pct = p * 100.0
    if pct >= 10:
        return f"{pct:.1f}%"
    if pct >= 1:
        return f"{pct:.2f}%"
    return f"{pct:.3f}%"


def _strip_tradeup_appearance_suffix(name: str) -> str:
    """去掉名称末尾「 (崭新出厂)」等外观后缀（兼容旧缓存；新结果本身不再带此后缀）。"""
    s = (name or "").rstrip()
    for ap in APPEARANCE:
        suf = f" ({ap})"
        if s.endswith(suf):
            return s[: -len(suf)].rstrip()
    return s


def _strip_substrate_name_for_simulation_import(name: str) -> str:
    """配方底物名常带「 (久经沙场)」或「 (Field-Tested)」；模拟搜索用无外观后缀的展示名。"""
    s = _strip_tradeup_appearance_suffix(name)
    for en in APPEARANCE_MAP:
        suf = f" ({en})"
        if s.endswith(suf):
            s = s[: -len(suf)].rstrip()
            break
    return s


def _substrate_float_value_display_str(raw: object, *, parsed: float) -> str:
    """与配方管理页底物表一致：固定 18 位小数，避免 ``format_float_shortest`` 缩短位数。"""
    if isinstance(raw, str):
        t = raw.strip().replace("。", ".")
        if t:
            try:
                if float(t) == parsed:
                    return t
            except ValueError:
                pass
    return f"{parsed:.18f}"


_SIM_WEAPON_BOX_UNKNOWN = "未关联武器箱"
_SIM_RESULTS_GROUP_GRID_COL_STRETCH_CLEAR = 32


def _simulation_row_price_yuan(r: dict) -> float:
    p = r.get("price", 0)
    if isinstance(p, (int, float)):
        return float(p)
    try:
        return float(p)
    except (TypeError, ValueError):
        return 0.0


def _group_simulation_rows_by_weapon_box(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """按武器箱分组，并按每组最高价产物从高到低排列。"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        wb = (r.get("weapon_box") or "").strip()
        key = wb if wb else _SIM_WEAPON_BOX_UNKNOWN
        groups.setdefault(key, []).append(r)
    for lst in groups.values():
        lst.sort(
            key=lambda x: (
                -_simulation_row_price_yuan(x),
                str(x.get("name", "")),
            )
        )
    keys = sorted(
        (k for k in groups if k != _SIM_WEAPON_BOX_UNKNOWN),
        key=lambda k: (
            -max((_simulation_row_price_yuan(r) for r in groups[k]), default=0.0),
            k,
        ),
    )
    out: list[tuple[str, list[dict]]] = [(k, groups[k]) for k in keys]
    if _SIM_WEAPON_BOX_UNKNOWN in groups:
        out.append((_SIM_WEAPON_BOX_UNKNOWN, groups[_SIM_WEAPON_BOX_UNKNOWN]))
    return out


def _simulation_price_outcome(
    product_price: float | None,
    recipe_cost: float | None,
) -> str:
    if product_price is None or product_price <= 0 or recipe_cost is None or recipe_cost <= 0:
        return "neutral"
    if product_price > recipe_cost:
        return "profit"
    if product_price < recipe_cost:
        return "loss"
    return "neutral"


def _apply_wear_zone_appearance_badge(label: QLabel, wear01: float | None) -> None:
    """外观角标：与自定义采集磨损条、库存磨损徽标相同的 ``WEAR_ZONE_COLORS`` 分区。"""
    if wear01 is None:
        label.hide()
        return
    zi = wear_zone_index(wear01)
    if zi is None:
        label.hide()
        return
    text = APPEARANCE[zi]
    bg = WEAR_ZONE_COLORS[zi]
    fg = INVENTORY_WEAR_BADGE_TEXT_COLOR
    label.setText(text)
    label.setStyleSheet(
        f"QLabel#alchemySimulationAppearanceBadge {{"
        f"background-color: {bg}; color: {fg};"
        f"border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600;"
        f"}}"
    )
    label.adjustSize()
    label.show()


def _sim_result_label_transparent(lb: QLabel) -> None:
    lb.setAutoFillBackground(False)
    lb.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)


class _SimulationResultCard(QFrame):
    """模拟产物：与底物卡同风格的图区 + 名称 + 磨损 + 概率。"""

    def __init__(
        self,
        skin_template: SkinTemplate,
        float_val: float,
        prob: float,
        display_name: str,
        card_fixed_width: int,
        parent=None,
        *,
        product_price: float | None = None,
        recipe_cost: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("alchemySimulationResultCard")
        self.setFixedHeight(_SIM_RESULT_CARD_HEIGHT)
        self.setFixedWidth(max(1, int(card_fixed_width)))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._quality_badge = QLabel(self)
        self._quality_badge.setObjectName("alchemySimulationQualityBadge")
        self._quality_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._quality_badge.hide()

        self._appearance_badge = QLabel(self)
        self._appearance_badge.setObjectName("alchemySimulationAppearanceBadge")
        self._appearance_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._appearance_badge.hide()

        root = QVBoxLayout(self)
        # 底边略小于顶边，减轻概率行下方「垫高」感
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(6)

        self._weapon_image = WeaponCardImageArea(
            self,
            area_height=_SIM_IMAGE_AREA_H,
            icon_width=_SIM_WEAPON_ICON_W_PX,
            icon_height=_SIM_WEAPON_ICON_H_PX,
            gradient_top_palette_role=QPalette.ColorRole.Window,
        )
        root.addWidget(self._weapon_image)

        self._name_label = QLabel(_strip_tradeup_appearance_suffix(display_name))
        self._name_label.setObjectName("alchemySimulationResultName")
        self._name_label.setWordWrap(True)
        self._name_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._name_label.setMaximumHeight(44)

        bar_w = max(100, int(card_fixed_width) - 20)
        fv = float(float_val)
        wear_float_row = QWidget()
        wear_float_row.setObjectName("alchemySimulationResultWearFloatRow")
        wear_float_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        wear_float_row.setMaximumWidth(bar_w)
        wfr = QHBoxLayout(wear_float_row)
        wfr.setContentsMargins(0, 0, 0, 0)
        wfr.setSpacing(0)
        wear_prefix = QLabel("磨损：")
        wear_prefix.setObjectName("alchemySimulationResultWearFloatPrefix")
        wear_prefix.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        wear_prefix.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        wear_prefix.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        _sim_result_label_transparent(wear_prefix)
        wear_value = QLabel(f"{fv:.18f}")
        wear_value.setObjectName("alchemySimulationResultWearFloatValue")
        wear_value.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        _sim_result_label_transparent(wear_value)
        wear_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        wear_value.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        wear_value.setCursor(Qt.CursorShape.IBeamCursor)
        wear_value.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        wfr.addWidget(wear_prefix, 0, Qt.AlignmentFlag.AlignVCenter)
        wfr.addWidget(wear_value, 1, Qt.AlignmentFlag.AlignVCenter)

        self._price_line = QLabel(self)
        self._price_line.setObjectName("alchemySimulationResultPriceLine")
        self._price_line.setProperty(
            "priceOutcome",
            _simulation_price_outcome(product_price, recipe_cost),
        )
        self._price_line.setText(_format_sim_price_prefix_yuan(product_price))

        # 名称 / 磨损 / 价格单独成块且 spacing=0，避免根布局 spacing 在行间插入空隙
        _meta = QWidget()
        _meta.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        _meta_l = QVBoxLayout(_meta)
        _meta_l.setContentsMargins(0, 0, 0, 0)
        _meta_l.setSpacing(0)
        _meta_l.addWidget(self._name_label)
        _meta_l.addWidget(wear_float_row, 0, Qt.AlignmentFlag.AlignLeft)
        _meta_l.addWidget(self._price_line, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(_meta)

        self._wear_legend = WearScaleLegendWidget(fv, self, bar_width=bar_w)
        self._wear_legend.setObjectName("alchemySimulationWearScaleLegend")

        self._prob_label = QLabel(f"概率 {_format_sim_prob(prob)}")
        self._prob_label.setObjectName("alchemySimulationResultProb")
        self._prob_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

        # 刻度与概率之间不用根布局 spacing(6)，收紧纵向空白
        _legend_prob = QWidget()
        _legend_prob.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        _lp = QVBoxLayout(_legend_prob)
        _lp.setContentsMargins(0, 0, 0, 0)
        _lp.setSpacing(2)
        _lp.addWidget(self._wear_legend, 0, Qt.AlignmentFlag.AlignLeft)
        _lp.addWidget(self._prob_label, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(_legend_prob)

        self._apply_weapon_image(skin_template)
        self._update_result_quality_badge(skin_template)
        qh = tradeup_display_quality(skin_template)
        lh, th = line_and_tint_for_quality_cn(qh)
        self._weapon_image.set_quality_colors(lh, th)
        _apply_wear_zone_appearance_badge(self._appearance_badge, float_val)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_result_corner_badges()

    def _reposition_result_corner_badges(self) -> None:
        self._appearance_badge.adjustSize()
        self._appearance_badge.move(8, 8)
        self._quality_badge.adjustSize()
        self._quality_badge.move(
            max(8, self.width() - self._quality_badge.width() - 8),
            8,
        )
        self._appearance_badge.raise_()
        self._quality_badge.raise_()

    def _update_result_quality_badge(self, tpl: SkinTemplate) -> None:
        q = tradeup_display_quality(tpl)
        bg = _SIM_QUALITY_BG.get(q)
        fg = _SIM_QUALITY_TEXT_COLOR.get(q, "#ffffff")
        if not bg:
            self._quality_badge.hide()
            return
        self._quality_badge.setText(q)
        self._quality_badge.setStyleSheet(
            f"QLabel#alchemySimulationQualityBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600;"
            f"}}"
        )
        self._quality_badge.adjustSize()
        self._quality_badge.show()

    def _apply_weapon_image(self, tpl: SkinTemplate) -> None:
        path = weapon_image_path_from_skin_template(tpl)
        if path:
            pix = _load_local_image_pixmap(path)
            if pix and not pix.isNull():
                self._weapon_image.set_weapon_pixmap(pix)
                return
            self._weapon_image.set_weapon_pixmap(None)
            self._weapon_image.set_icon_placeholder_text("图片加载失败")
        else:
            self._weapon_image.set_weapon_pixmap(None)
            self._weapon_image.set_icon_placeholder_text("无本地图")

    def refresh_weapon_image_palette(self) -> None:
        self._weapon_image.refresh_for_palette()


class _WearDoubleValidator(QDoubleValidator):
    """去首尾空白/换行、中文句号与常见 Unicode 减号后再交给 ``QDoubleValidator``。

    裸用 ``QDoubleValidator`` 时，从网页/记事本 Ctrl+V 常带入 ``\\r\\n``、全角空格等，
    整段校验为 Invalid，表现为无法粘贴。
    """

    _STRIP = " \t\r\n\u3000\u00a0"

    def validate(self, input_str: str, pos: int):
        s = input_str or ""
        lead = len(s) - len(s.lstrip(self._STRIP))
        trail = len(s) - len(s.rstrip(self._STRIP))
        core = (
            s.strip(self._STRIP)
            .replace("。", ".")
            .replace("−", "-")
            .replace("－", "-")
        )
        if pos <= lead:
            new_pos = 0
        elif pos >= len(s) - trail:
            new_pos = len(core)
        else:
            new_pos = pos - lead
        new_pos = max(0, min(len(core), new_pos))
        return super().validate(core, new_pos)

    def fixup(self, input: str) -> str:
        s = (
            (input or "")
            .strip(self._STRIP)
            .replace("。", ".")
            .replace("−", "-")
            .replace("－", "-")
        )
        return super().fixup(s)


class _SimulationSubstrateCard(QFrame):
    """单格底物：图区 + 皮肤搜索 + 磨损；左上角外观角标、右上角品质。"""

    def __init__(
        self,
        index: int,
        parent=None,
        *,
        min_width: int,
        card_fixed_width: int,
        allowed_qualities: frozenset[str] | None,
        candidate_popup_above: bool,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("alchemySimulationCard")
        self.setFixedHeight(_SIM_CARD_FIXED_HEIGHT)
        self.setFixedWidth(max(1, int(card_fixed_width)))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._index = index
        self._template: SkinTemplate | None = None
        self._icon_gen = 0

        self._quality_badge = QLabel(self)
        self._quality_badge.setObjectName("alchemySimulationQualityBadge")
        self._quality_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._quality_badge.hide()

        self._appearance_badge = QLabel(self)
        self._appearance_badge.setObjectName("alchemySimulationAppearanceBadge")
        self._appearance_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._appearance_badge.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self._weapon_image = WeaponCardImageArea(
            self,
            area_height=_SIM_IMAGE_AREA_H,
            icon_width=_SIM_WEAPON_ICON_W_PX,
            icon_height=_SIM_WEAPON_ICON_H_PX,
            gradient_top_palette_role=QPalette.ColorRole.Window,
        )
        self._weapon_image.set_weapon_pixmap(None)
        self._weapon_image.set_icon_placeholder_text("请选择底物")
        root.addWidget(self._weapon_image)

        self._price_line = QLabel(self)
        self._price_line.setObjectName("alchemySimulationSubstratePriceLine")
        self.set_substrate_price_yuan(None)
        root.addWidget(self._price_line)

        self._skin_search = SkinSearchField(
            self,
            min_width=min_width,
            allowed_qualities=allowed_qualities,
            candidate_popup_above=candidate_popup_above,
            candidate_list_object_name="alchemySimulationSkinCandidateList",
            line_edit_object_name="alchemySimulationSkinSearchEdit",
            include_weapon_box_search=False,
            auto_pick_first_on_focus_out=True,
        )
        self._skin_search.skin_resolved.connect(self._on_skin_resolved)
        root.addWidget(self._skin_search)

        self._wear_edit = QLineEdit(self)
        self._wear_edit.setObjectName("alchemySimulationWearEdit")
        self._wear_edit.setPlaceholderText("请输入磨损")
        self._wear_edit.setReadOnly(True)
        self._wear_edit.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # 仅可选皮肤且可编辑磨损时显示 ×；只读时关闭，避免程序 clear() 后叉仍残留
        self._wear_edit.setClearButtonEnabled(False)
        root.addWidget(self._wear_edit)

        self._wear_commit_debounce = QTimer(self)
        self._wear_commit_debounce.setSingleShot(True)
        self._wear_commit_debounce.setInterval(0)
        self._wear_commit_debounce.timeout.connect(self._on_wear_focus_out_deferred)
        self._wear_edit.returnPressed.connect(self._on_wear_return_pressed)
        self._wear_edit.textChanged.connect(self._sync_appearance_badge_from_wear)
        self._wear_edit.installEventFilter(self)

    @staticmethod
    def _scroll_wear_line_to_logical_start(le: QLineEdit) -> None:
        """与 SkinSearchField 一致：光标到开头并取消选择，使长文本从左侧可见。"""
        le.setCursorPosition(0)
        le.deselect()

    def _defer_wear_line_scroll_to_start(self) -> None:
        def _go() -> None:
            if not self.isVisible():
                return
            self._scroll_wear_line_to_logical_start(self._wear_edit)

        QTimer.singleShot(0, _go)

    def set_substrate_price_yuan(self, value: float | None) -> None:
        """产物价格表中的单价；未知或 0 显示为 ``￥-``。"""
        self._price_line.setText(_format_sim_price_prefix_yuan(value))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._wear_edit:
            if event.type() == QEvent.Type.KeyPress:
                ke = event
                if isinstance(ke, QKeyEvent) and ke.key() in (
                    Qt.Key.Key_Return,
                    Qt.Key.Key_Enter,
                ):
                    # Invalid 时 Qt 往往不发 returnPressed；在此先校验，不合法则 toast+清空并吃掉按键
                    if not self._wear_edit.isReadOnly() and self._template is not None:
                        if not self._validate_wear_on_commit():
                            self._wear_edit.clearFocus()
                            return True
            elif event.type() == QEvent.Type.FocusOut:
                self._wear_commit_debounce.start()
        return super().eventFilter(watched, event)

    def _validate_wear_on_commit(self) -> bool:
        """不合法时已 toast 并清空；返回 False 表示刚处理过非法输入。"""
        if not self.isVisible():
            return True
        tpl = self._template
        if tpl is None or self._wear_edit.isReadOnly():
            return True
        raw = self._wear_edit.text().strip()
        if not raw:
            return True
        wear = _parse_wear_value(raw)
        if wear is None:
            show_toast(self, "请输入有效磨损数值", style="warning")
            self._wear_edit.blockSignals(True)
            self._wear_edit.clear()
            self._wear_edit.blockSignals(False)
            self._repolish_wear_clear_button()
            self._sync_appearance_badge_from_wear()
            return False
        if not _wear_in_template_range(tpl, wear):
            lo, hi = float(tpl.min_float), float(tpl.max_float)
            show_toast(
                self,
                f"磨损需在 {format_float_shortest(lo)}～{format_float_shortest(hi)} 之间",
                style="warning",
            )
            self._wear_edit.blockSignals(True)
            self._wear_edit.clear()
            self._wear_edit.blockSignals(False)
            self._repolish_wear_clear_button()
            self._sync_appearance_badge_from_wear()
            return False
        return True

    def _on_wear_return_pressed(self) -> None:
        # 合法/空：由 eventFilter 放行后仍会进此槽；再校验一次并失焦
        self._validate_wear_on_commit()
        self._defer_wear_line_scroll_to_start()
        self._wear_edit.clearFocus()

    def _on_wear_focus_out_deferred(self) -> None:
        if QApplication.focusWidget() is self._wear_edit:
            return
        self._validate_wear_on_commit()
        self._defer_wear_line_scroll_to_start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_substrate_corner_badges()

    def _reposition_substrate_corner_badges(self) -> None:
        self._appearance_badge.adjustSize()
        self._appearance_badge.move(8, 8)
        self._quality_badge.adjustSize()
        self._quality_badge.move(
            max(8, self.width() - self._quality_badge.width() - 8),
            8,
        )
        self._appearance_badge.raise_()
        self._quality_badge.raise_()

    def _sync_appearance_badge_from_wear(self) -> None:
        wear_ok: float | None = None
        tpl = self._template
        if tpl is not None and not self._wear_edit.isReadOnly():
            raw = self._wear_edit.text().strip()
            if raw:
                parsed = _parse_wear_value(raw)
                if parsed is not None and _wear_in_template_range(tpl, parsed):
                    wear_ok = parsed
        _apply_wear_zone_appearance_badge(self._appearance_badge, wear_ok)
        self._reposition_substrate_corner_badges()

    def _sync_wear_validator(self) -> None:
        tpl = self._template
        if tpl is None:
            self._wear_edit.setValidator(None)
            return
        v = _WearDoubleValidator(
            float(tpl.min_float),
            float(tpl.max_float),
            _SIM_WEAR_MAX_DECIMALS,
            self._wear_edit,
        )
        v.setLocale(QLocale.c())
        v.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._wear_edit.setValidator(v)

    def _update_quality_badge(self, quality: str | None) -> None:
        if not quality:
            self._quality_badge.hide()
            self._reposition_substrate_corner_badges()
            return
        bg = _SIM_QUALITY_BG.get(quality)
        fg = _SIM_QUALITY_TEXT_COLOR.get(quality, "#ffffff")
        if not bg:
            self._quality_badge.hide()
            self._reposition_substrate_corner_badges()
            return
        self._quality_badge.setText(quality)
        self._quality_badge.setStyleSheet(
            f"QLabel#alchemySimulationQualityBadge {{"
            f"background-color: {bg}; color: {fg};"
            f"border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600;"
            f"}}"
        )
        self._quality_badge.adjustSize()
        self._quality_badge.show()
        self._reposition_substrate_corner_badges()

    def _on_skin_resolved(self, tpl: object | None) -> None:
        self._icon_gen += 1
        if not isinstance(tpl, SkinTemplate):
            self._template = None
            self._wear_edit.blockSignals(True)
            self._wear_edit.clear()
            self._wear_edit.setReadOnly(True)
            self._wear_edit.setPlaceholderText("请输入磨损")
            self._wear_edit.setValidator(None)
            self._wear_edit.setClearButtonEnabled(False)
            self._wear_edit.blockSignals(False)
            self._wear_edit.update()
            # 清空底物后须恢复默认品质渐变；否则内联 qss 仍沿用上一皮肤的 tint/底条色
            lh_d, th_d = line_and_tint_for_quality_cn("")
            self._weapon_image.set_quality_colors(lh_d, th_d)
            self._weapon_image.set_weapon_pixmap(None)
            self._weapon_image.set_icon_placeholder_text("请选择底物")
            self._update_quality_badge(None)
            self.set_substrate_price_yuan(None)
            self._sync_appearance_badge_from_wear()
            return

        self._template = tpl
        self.set_substrate_price_yuan(None)
        lo, hi = float(tpl.min_float), float(tpl.max_float)
        ph = f"{format_float_shortest(lo)}~{format_float_shortest(hi)}"
        self._wear_edit.blockSignals(True)
        self._wear_edit.clear()
        self._wear_edit.setReadOnly(False)
        self._wear_edit.setPlaceholderText(ph)
        self._wear_edit.setClearButtonEnabled(True)
        self._wear_edit.blockSignals(False)
        self._wear_edit.update()
        self._sync_wear_validator()
        self._update_quality_badge(tradeup_display_quality(tpl))

        gen = self._icon_gen
        path = weapon_image_path_from_skin_template(tpl)
        if gen != self._icon_gen:
            self._sync_appearance_badge_from_wear()
            return
        qh = tradeup_display_quality(tpl)
        lh, th = line_and_tint_for_quality_cn(qh)
        self._weapon_image.set_quality_colors(lh, th)
        if path:
            pix = _load_local_image_pixmap(path)
            if pix and not pix.isNull():
                self._weapon_image.set_weapon_pixmap(pix)
            else:
                self._weapon_image.set_weapon_pixmap(None)
                self._weapon_image.set_icon_placeholder_text("图片加载失败")
        else:
            self._weapon_image.set_weapon_pixmap(None)
            self._weapon_image.set_icon_placeholder_text("无本地图")

        self._sync_appearance_badge_from_wear()

    def substrate_index(self) -> int:
        return self._index

    def skin_template(self) -> SkinTemplate | None:
        return self._template

    def selected_display_name(self) -> str:
        return (self._skin_search.selected_from_dropdown() or "").strip()

    def wear_text(self) -> str:
        return self._wear_edit.text().strip()

    def prepare_names_index(self) -> None:
        self._skin_search.ensure_names_loaded()

    def set_allowed_qualities(self, allowed: frozenset[str] | None) -> None:
        self._skin_search.set_allowed_qualities(allowed)

    def set_excluded_qualities(self, excluded: frozenset[str] | None) -> None:
        self._skin_search.set_excluded_qualities(excluded)

    def set_template_extra_predicate(
        self, pred: Callable[[SkinTemplate], bool] | None
    ) -> None:
        self._skin_search.set_template_extra_predicate(pred)

    def set_candidate_popup_above(self, above: bool) -> None:
        self._skin_search.set_candidate_popup_above(above)

    def refresh_weapon_image_palette_if_any(self) -> None:
        self._weapon_image.refresh_for_palette()

    def _repolish_wear_clear_button(self) -> None:
        """程序 clear() 后 Qt 常不刷新 ×；重绑清除键并 update。"""
        w = self._wear_edit
        if not w.isReadOnly() and w.isClearButtonEnabled():
            w.setClearButtonEnabled(False)
            w.setClearButtonEnabled(True)
        w.update()

    def refresh_clear_buttons(self) -> None:
        self._skin_search.refresh_clear_button()
        self._repolish_wear_clear_button()

    def reset_substrate(self) -> None:
        self._skin_search.clear_for_next_entry()
        self.set_substrate_price_yuan(None)

    def restore_substrate_snapshot(self, name: str, wear: str) -> None:
        """从名称 + 磨损字符串恢复；调用方应在布局后执行 ``revalidate_search_selection``。"""
        self._skin_search.clear_for_next_entry()
        nm = (name or "").strip()
        if nm:
            self._skin_search.restore_picked_skin(nm)
            w = (wear or "").strip()
            if w:
                self._wear_edit.blockSignals(True)
                self._wear_edit.setText(w)
                self._wear_edit.blockSignals(False)
                self._defer_wear_line_scroll_to_start()
        self._sync_appearance_badge_from_wear()
        self.set_substrate_price_yuan(None)

    def revalidate_search_selection(self) -> None:
        self._skin_search.revalidate_current_selection()
        self._sync_appearance_badge_from_wear()


class AlchemySimulationPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("alchemySimulationPage")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        main_layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)
        self._page_title_icon_label = QLabel(self)
        self._page_title_icon_label.setObjectName("contentPageTitleIcon")
        self._page_title_icon_label.setFixedSize(28, 28)
        self._page_title_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self._page_title_icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title_label = QLabel("炼金模拟")
        self._title_label.setObjectName("alchemySimulationPageTitle")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title_label.setFont(title_font)
        self._title_label.setAttribute(Qt.WA_TranslucentBackground)
        top_row.addWidget(self._title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._price_basis_hint = QLabel("基于区间底价估计")
        self._price_basis_hint.setObjectName("alchemySimulationPriceBasisHint")
        self._price_basis_hint.setAttribute(Qt.WA_TranslucentBackground)
        top_row.addWidget(self._price_basis_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        top_row.addStretch(1)

        self._mode_switch = SegmentedCheckSwitch(
            container_object_name="alchemySimulationModeSegmented",
            slider_object_name="alchemySimulationModeSlider",
            segments=(
                ("alchemySimSegmentFive", "五合一"),
                ("alchemySimSegmentTen", "十合一"),
            ),
        )
        self._mode_five_btn = self._mode_switch.buttons[0]
        self._mode_ten_btn = self._mode_switch.buttons[1]
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._mode_five_btn)
        self._mode_group.addButton(self._mode_ten_btn)
        self._mode_group.setExclusive(True)
        self._mode_ten_btn.setChecked(True)
        self._mode_five_btn.clicked.connect(self._on_mode_changed)
        self._mode_ten_btn.clicked.connect(self._on_mode_changed)
        top_row.addWidget(self._mode_switch, 0, Qt.AlignmentFlag.AlignVCenter)

        self._clear_data_btn = QPushButton("清除数据")
        self._clear_data_btn.setObjectName("alchemySimulationClearBtn")
        self._clear_data_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_data_btn.setAutoDefault(False)
        self._clear_data_btn.setDefault(False)
        self._clear_data_btn.clicked.connect(self._on_clear_data_clicked)
        top_row.addWidget(self._clear_data_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._save_recipe_btn = QPushButton("保存配方")
        self._save_recipe_btn.setObjectName("alchemySimulationSaveRecipeBtn")
        self._save_recipe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_recipe_btn.setAutoDefault(False)
        self._save_recipe_btn.setDefault(False)
        self._save_recipe_btn.setEnabled(False)
        self._save_recipe_btn.clicked.connect(self._on_save_recipe_clicked)
        top_row.addWidget(self._save_recipe_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._calc_btn = QPushButton("开始模拟")
        self._calc_btn.setObjectName("alchemySimulationCalcBtn")
        self._calc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._calc_btn.setAutoDefault(False)
        self._calc_btn.setDefault(False)
        self._calc_btn.clicked.connect(self._on_start_calculate_clicked)
        top_row.addWidget(self._calc_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        main_layout.addLayout(top_row)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("alchemySimulationScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )

        self._scroll_inner = QWidget()
        self._scroll_inner.setObjectName("alchemySimulationScrollInner")
        self._scroll_inner.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        _sil = QVBoxLayout(self._scroll_inner)
        _sil.setContentsMargins(0, 0, 0, 0)
        _sil.setSpacing(0)

        self._grid_host = QWidget()
        self._grid_host.setObjectName("alchemySimulationGridHost")
        # 垂直不吞余量：避免底物网格底部 stretch 行把整块区撑高，与下方「模拟结果」之间出现大空白
        self._grid_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(_SIM_GRID_SPACING)
        self._grid.setVerticalSpacing(_SIM_GRID_SPACING)
        _sil.addWidget(self._grid_host)

        self._results_section = QWidget()
        self._results_section.setObjectName("alchemySimulationResultsSection")
        self._results_section.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        _rv = QVBoxLayout(self._results_section)
        _rv.setContentsMargins(0, 24, 0, 0)
        _rv.setSpacing(10)

        self._results_line = QFrame()
        self._results_line.setObjectName("alchemySimulationResultsDivider")
        self._results_line.setFixedHeight(1)
        self._results_line.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        _rv.addWidget(self._results_line)

        self._results_header = QWidget()
        self._results_header.setObjectName("alchemySimulationResultsHeader")
        _rh = QHBoxLayout(self._results_header)
        _rh.setContentsMargins(0, 0, 0, 0)
        _rh.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        # 标题 + 摘要合并为单个富文本 QLabel；横向滚动避免窄窗口断行
        self._results_header_scroll = QScrollArea()
        self._results_header_scroll.setObjectName("alchemySimulationResultsHeaderScroll")
        self._results_header_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._results_header_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._results_header_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._results_header_scroll.setWidgetResizable(False)
        self._results_header_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        self._results_header_label = QLabel(self)
        self._results_header_label.setObjectName("alchemySimulationResultsHeaderLine")
        self._results_header_label.setTextFormat(Qt.TextFormat.RichText)
        self._results_header_label.setWordWrap(False)
        self._results_header_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._results_header_scroll.setWidget(self._results_header_label)
        _rh.addWidget(self._results_header_scroll, 1, Qt.AlignmentFlag.AlignVCenter)
        _rv.addWidget(self._results_header)

        self._apply_results_summary(None)

        self._results_grid_host = QWidget()
        self._results_grid_host.setObjectName("alchemySimulationResultsGridHost")
        self._results_grid_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self._results_groups_layout = QVBoxLayout(self._results_grid_host)
        self._results_groups_layout.setContentsMargins(0, 0, 0, 0)
        self._results_groups_layout.setSpacing(20)
        _rv.addWidget(self._results_grid_host)

        _sil.addWidget(self._results_section)
        self._results_section.hide()

        self._scroll.setWidget(self._scroll_inner)
        main_layout.addWidget(self._scroll, 1)

        self._cards: list[_SimulationSubstrateCard] = []
        self._result_cards: list[_SimulationResultCard] = []
        self._result_groups: list[dict] = []
        _cw = ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH
        for i in range(_SIM_MAX_CARDS):
            card = _SimulationSubstrateCard(
                i,
                self._grid_host,
                min_width=ALCHEMY_SIMULATION_SKIN_SEARCH_MIN_WIDTH,
                card_fixed_width=_cw,
                allowed_qualities=None,
                candidate_popup_above=False,
            )
            self._cards.append(card)

        # 五合一 / 十合一 各自独立的槽位快照（切换模式时先存后换筛选再恢复）
        self._slot_snapshots_five: list[tuple[str, str]] = [("", "") for _ in range(5)]
        self._slot_snapshots_ten: list[tuple[str, str]] = [("", "") for _ in range(_SIM_MAX_CARDS)]
        self._mode_switch_last_five: bool | None = None
        # 各模式独立的模拟结果缓存（切换模式时恢复对应产物列表）
        self._simulation_rows_five: list[dict] | None = None
        self._simulation_rows_ten: list[dict] | None = None
        self._simulation_metrics_five: dict | None = None
        self._simulation_metrics_ten: dict | None = None
        self._simulation_substrate_prices_five: list[float] | None = None
        self._simulation_substrate_prices_ten: list[float] | None = None
        self._pending_simulation_substrates: list[tuple[SkinTemplate, float]] | None = None
        self._fetch_worker: QObject | None = None
        self._simulation_price_fetch_busy = False

        QTimer.singleShot(0, self._apply_mode_to_cards)

    def _is_five_mode(self) -> bool:
        return self._mode_five_btn.isChecked()

    def _visible_slot_count(self) -> int:
        return 5 if self._is_five_mode() else _SIM_MAX_CARDS

    def _defocus_substrate_line_edits_if_any(self) -> None:
        """切换模式时程序化改写字段会抢走焦点，从底物区输入框移开。"""
        fw = QApplication.focusWidget()
        if fw is None:
            return
        if not self._grid_host.isAncestorOf(fw):
            return
        if isinstance(fw, QLineEdit):
            fw.clearFocus()

    def _on_mode_changed(self) -> None:
        self._apply_mode_to_cards()
        self._defocus_substrate_line_edits_if_any()
        self._mode_switch.sync_mode_slider(animate=True)

        def _defer_after_mode_change() -> None:
            self._sync_results_panel_from_cache()
            self._apply_simulation_substrate_prices_from_cache()
            self._update_save_recipe_button_state()
            self._relayout_scroll_content()
            self._defocus_substrate_line_edits_if_any()

        QTimer.singleShot(0, _defer_after_mode_change)

    @staticmethod
    def _snapshot_card(card: _SimulationSubstrateCard) -> tuple[str, str]:
        return (card.selected_display_name(), card.wear_text())

    def _apply_mode_to_cards(self) -> None:
        now_five = self._is_five_mode()
        prev_five = self._mode_switch_last_five

        if prev_five is not None:
            if prev_five:
                self._slot_snapshots_five = [
                    self._snapshot_card(self._cards[i]) for i in range(5)
                ]
            else:
                self._slot_snapshots_ten = [
                    self._snapshot_card(self._cards[i]) for i in range(_SIM_MAX_CARDS)
                ]

        self._mode_switch_last_five = now_five

        for card in self._cards:
            if now_five:
                card.set_excluded_qualities(None)
                card.set_allowed_qualities(_COVERT_ONLY)
                card.set_template_extra_predicate(_five_mode_search_predicate)
            else:
                card.set_template_extra_predicate(None)
                card.set_allowed_qualities(None)
                card.set_excluded_qualities(_TEN_EXCLUDED_QUALITIES)

        if now_five:
            for i in range(5):
                self._cards[i].restore_substrate_snapshot(*self._slot_snapshots_five[i])
            for i in range(5, _SIM_MAX_CARDS):
                self._cards[i].reset_substrate()
        else:
            for i in range(_SIM_MAX_CARDS):
                self._cards[i].restore_substrate_snapshot(*self._slot_snapshots_ten[i])

        for i in range(self._visible_slot_count()):
            self._cards[i].revalidate_search_selection()

    def _relayout_simulation_grid(self) -> None:
        viewport_w = max(1, self._scroll.viewport().width())
        s = _SIM_GRID_SPACING
        card_w = ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH
        # 固定卡宽；视口能摆下 n 列：n*card_w + (n-1)*s <= viewport_w
        slot = card_w + s
        max_fit = max(1, (viewport_w + s) // slot)
        n_vis = self._visible_slot_count()
        cols = min(max_fit, n_vis)
        ten = not self._is_five_mode()

        while self._grid.count():
            self._grid.takeAt(0)

        # 清掉上次 relayout 遗留的 row stretch，避免行高被旧 stretch 拉高
        for r in range(_SIM_MAX_CARDS + 2):
            self._grid.setRowStretch(r, 0)

        for i in range(_SIM_MAX_CARDS):
            card = self._cards[i]
            show = i < n_vis
            card.setVisible(show)
            if not show:
                continue
            r, c = divmod(i, cols)
            self._grid.addWidget(
                card, r, c, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            card.set_candidate_popup_above(bool(ten and r >= 1))

        # 不在此处 setRowStretch：否则最大化时空行吃满高度，底物与下方模拟结果之间会出现大块空白。
        # 卡片已用 AlignTop 放入格内；_grid_host 为垂直 Maximum，高度随内容收缩。

        for c in range(cols):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(cols, 1)

        for i in range(n_vis):
            self._cards[i].refresh_clear_buttons()

    def _clear_results_ui_only(self) -> None:
        """仅移除产物卡片控件，不改动各模式下的结果缓存。"""
        lay = self._results_groups_layout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._result_groups.clear()
        self._result_cards.clear()
        self._apply_results_summary(None)

    def _sync_results_panel_from_cache(self) -> None:
        """按当前五合一/十合一模式从缓存重建产物区 UI。"""
        rows = (
            self._simulation_rows_five
            if self._is_five_mode()
            else self._simulation_rows_ten
        )
        metrics = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        self._rebuild_results_from_rows(rows)
        self._apply_results_summary(metrics)
        self._update_save_recipe_button_state()

    def _rebuild_results_from_rows(self, rows: list[dict] | None) -> None:
        self._clear_results_ui_only()
        if not rows:
            self._results_section.hide()
            return
        cw = ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH
        metrics = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        try:
            recipe_cost = float(metrics.get("cost")) if metrics else None
        except (TypeError, ValueError):
            recipe_cost = None
        grouped = _group_simulation_rows_by_weapon_box(rows)
        for box_title, group_rows in grouped:
            group_wrap = QWidget()
            group_wrap.setObjectName("alchemySimulationResultsGroup")
            group_lay = QVBoxLayout(group_wrap)
            group_lay.setContentsMargins(0, 0, 0, 0)
            group_lay.setSpacing(10)

            n_in_box = len(group_rows)
            title_lb = QLabel(f"{box_title}（{n_in_box}）")
            title_lb.setObjectName("alchemySimulationResultsGroupTitle")
            title_lb.setWordWrap(True)
            group_lay.addWidget(title_lb)

            grid_host = QWidget()
            grid_host.setObjectName("alchemySimulationResultsGroupGridHost")
            grid_host.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Maximum,
            )
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(_SIM_GRID_SPACING)
            grid.setVerticalSpacing(_SIM_GRID_SPACING)
            group_lay.addWidget(grid_host)

            cards: list[_SimulationResultCard] = []
            for row in group_rows:
                tpl = row["skin_template"]
                raw_p = row.get("price")
                product_price = (
                    float(raw_p) if isinstance(raw_p, (int, float)) else None
                )
                card = _SimulationResultCard(
                    tpl,
                    float(row["float_value"]),
                    float(row["prob"]),
                    str(row["name"]),
                    cw,
                    grid_host,
                    product_price=product_price,
                    recipe_cost=recipe_cost,
                )
                cards.append(card)
                self._result_cards.append(card)

            self._result_groups.append({"wrap": group_wrap, "grid": grid, "cards": cards})
            self._results_groups_layout.addWidget(group_wrap)

        self._results_section.setVisible(True)

    def _relayout_results_grid(self) -> None:
        if not self._result_groups:
            return
        viewport_w = max(1, self._scroll.viewport().width())
        s = _SIM_GRID_SPACING
        card_w = ALCHEMY_SIMULATION_GRID_COLUMN_MIN_WIDTH
        slot = card_w + s
        max_fit = max(1, (viewport_w + s) // slot)

        for g in self._result_groups:
            cards: list[_SimulationResultCard] = g["cards"]
            grid: QGridLayout = g["grid"]
            n_g = len(cards)
            if n_g == 0:
                continue
            cols_g = min(max_fit, max(1, n_g))

            while grid.count():
                grid.takeAt(0)

            for r in range(n_g + 2):
                grid.setRowStretch(r, 0)

            for i, card in enumerate(cards):
                r, c = divmod(i, cols_g)
                grid.addWidget(
                    card,
                    r,
                    c,
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                )

            for c in range(cols_g):
                grid.setColumnStretch(c, 0)
            grid.setColumnStretch(cols_g, 1)
            for c in range(cols_g + 1, _SIM_RESULTS_GROUP_GRID_COL_STRETCH_CLEAR):
                grid.setColumnStretch(c, 0)

    def _relayout_scroll_content(self) -> None:
        self._relayout_simulation_grid()
        self._relayout_results_grid()

    def _results_header_rich_html(self, recipe: dict | None) -> str:
        """单行富文本：标题色随 palette，摘要为绿/红（与原先 QLabel 样式一致）。"""
        self.ensurePolished()
        tc = self.palette().color(QPalette.ColorRole.WindowText).name()
        base_style = "font-size:17px;font-weight:700"
        if not recipe:
            return f'<span style="color:{tc};{base_style}">模拟结果</span>'
        sc = "#10b981" if recipe.get("rate", 0) >= 0 else "#ef4444"
        line = html.escape(format_recipe_summary_line(recipe))
        return (
            f'<span style="color:{tc};{base_style}">模拟结果：</span>  '
            f'<span style="color:{sc};{base_style}">{line}</span>'
        )

    def _apply_results_summary(self, recipe: dict | None) -> None:
        self._results_header_label.setText(self._results_header_rich_html(recipe))
        self._results_header_label.adjustSize()
        row_h = max(28, self._results_header_label.sizeHint().height())
        self._results_header_scroll.setFixedHeight(int(row_h))

    def _refresh_results_header_for_palette(self) -> None:
        if not self._results_section.isVisible():
            return
        m = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        self._apply_results_summary(m)

    def _simulation_set_calc_busy(self) -> None:
        self._calc_btn.setEnabled(False)
        self._calc_btn.setText("加载中...")
        self._save_recipe_btn.setEnabled(False)

    def _simulation_set_calc_idle(self) -> None:
        self._calc_btn.setEnabled(True)
        self._calc_btn.setText("开始模拟")
        self._update_save_recipe_button_state()

    def _validated_visible_substrates(
        self,
    ) -> tuple[list[tuple[SkinTemplate, float, str]], str | None]:
        cards = self._cards[: self._visible_slot_count()]
        rows: list[tuple[SkinTemplate, float, str]] = []
        for c in cards:
            tpl = c.skin_template()
            name = c.selected_display_name()
            wear = _parse_wear_value(c.wear_text())
            if tpl is None or not name or wear is None or not _wear_in_template_range(tpl, wear):
                return [], _SIM_SUBSTRATE_INVALID_MESSAGE
            rows.append((tpl, wear, name))

        qualities = {tradeup_display_quality(tpl) for tpl, _, _ in rows}
        if len(qualities) != 1:
            return [], "底物品质不一致，请使用同一品质"

        st_flags = {tpl.stat_trak for tpl, _, _ in rows}
        if len(st_flags) > 1:
            return [], "底物需全部为暗金或全部为非暗金"

        for tpl, _, name in rows:
            if not tpl.upper_skins:
                return [], f"「{name}」没有上级，无法炼金"
        return rows, None

    def _update_save_recipe_button_state(self) -> None:
        if self._simulation_price_fetch_busy:
            self._save_recipe_btn.setEnabled(False)
            return
        metrics = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        rows = (
            self._simulation_rows_five
            if self._is_five_mode()
            else self._simulation_rows_ten
        )
        prices = (
            self._simulation_substrate_prices_five
            if self._is_five_mode()
            else self._simulation_substrate_prices_ten
        )
        n = self._visible_slot_count()
        k_meta: int
        raw_k = metrics.get("simulation_slot_count") if metrics else None
        if raw_k is None:
            k_meta = n
        else:
            try:
                k_meta = int(raw_k)
            except (TypeError, ValueError):
                k_meta = n
        ok = bool(
            metrics is not None
            and rows
            and prices is not None
            and len(prices) == n
            and len(prices) == k_meta
            and k_meta == n
        )
        self._save_recipe_btn.setEnabled(ok)

    def _apply_simulation_substrate_prices_from_cache(self) -> None:
        """切换五/十合一时，按缓存恢复各槽位「价格：」显示。"""
        n = self._visible_slot_count()
        prices = (
            self._simulation_substrate_prices_five
            if self._is_five_mode()
            else self._simulation_substrate_prices_ten
        )
        if not prices or len(prices) != n:
            for i in range(n):
                self._cards[i].set_substrate_price_yuan(None)
            return
        for i in range(n):
            self._cards[i].set_substrate_price_yuan(prices[i])
        for i in range(n, _SIM_MAX_CARDS):
            self._cards[i].set_substrate_price_yuan(None)

    def _build_simulation_substrates_display(self) -> list[dict]:
        """与炼金保存配方 ``substrates_display`` 字段结构一致。"""
        metrics = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        if not metrics:
            return []
        n = self._visible_slot_count()
        raw_k = metrics.get("simulation_slot_count")
        if raw_k is None:
            k = n
        else:
            try:
                k = int(raw_k)
            except (TypeError, ValueError):
                k = n
        if k != n:
            return []
        prices = (
            self._simulation_substrate_prices_five
            if self._is_five_mode()
            else self._simulation_substrate_prices_ten
        )
        if not prices or len(prices) != k:
            return []
        out: list[dict] = []
        for i in range(k):
            card = self._cards[i]
            tpl = card.skin_template()
            if tpl is None:
                return []
            wear = _parse_wear_value(card.wear_text())
            if wear is None or not _wear_in_template_range(tpl, wear):
                return []
            out.append(
                {
                    "name": _skin_tradeup_display_name_with_appearance(tpl, float(wear)),
                    "float_value": float(wear),
                    "price": float(prices[i]),
                    "weapon_box": (
                        tpl.weapon_box_name[0] if tpl.weapon_box_name else ""
                    ),
                    "platform": "buff",
                    "purchase_link": None,
                }
            )
        return out

    def _collect_simulation_recipe_for_save(self) -> dict | None:
        metrics = (
            self._simulation_metrics_five
            if self._is_five_mode()
            else self._simulation_metrics_ten
        )
        rows = (
            self._simulation_rows_five
            if self._is_five_mode()
            else self._simulation_rows_ten
        )
        if not metrics or not rows:
            return None
        sub_disp = self._build_simulation_substrates_display()
        if not sub_disp:
            return None
        prod_disp = []
        for r in rows:
            tpl = r.get("skin_template")
            fv = float(r["float_value"])
            if isinstance(tpl, SkinTemplate):
                nm = _skin_tradeup_display_name_with_appearance(tpl, fv)
            else:
                nm = str(r.get("name", ""))
                app = SkinInstance.get_appearance(fv)
                if app and nm:
                    nm = f"{nm} ({app})"
            prod_disp.append(
                {
                    "name": nm,
                    "float_value": fv,
                    "prob": float(r["prob"]),
                    "weapon_box": str(r.get("weapon_box") or ""),
                    "price": float(r.get("price") or 0.0),
                }
            )
        prod_disp.sort(key=lambda x: -x["prob"])
        k_slots = int(metrics.get("simulation_slot_count") or len(sub_disp))
        return {
            "cost": float(metrics["cost"]),
            "expectation": float(metrics["expectation"]),
            "rate": float(metrics["rate"]),
            "break_even_rate": float(metrics["break_even_rate"]),
            "avg_nfv": float(metrics["avg_nfv"]),
            "simulation_slot_count": k_slots,
            "substrates_display": sub_disp,
            "products_display": prod_disp,
        }

    def _on_save_recipe_clicked(self) -> None:
        recipe = self._collect_simulation_recipe_for_save()
        if recipe is None:
            show_toast(self, "请先完成一次带价格的模拟", style="warning")
            return
        avg_nfv = float(recipe["avg_nfv"])
        try:
            targets = build_all_recipe_folder_pick_targets()
            dlg = MoveRecipeFolderDialog(
                self.window(),
                targets=targets,
                dialog_title="保存配方到",
                hint_text="请勾选一个目标文件夹，然后点击「确定」。",
                allow_create_folder=True,
                recipe_name_default=default_save_recipe_dialog_title(recipe),
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            fid = dlg.chosen_folder_id()
            save_recipe_file(
                recipe,
                rank=1,
                mode="simulation",
                norm_min=avg_nfv,
                norm_max=avg_nfv,
                folder_id=fid,
                title=dlg.chosen_recipe_title(),
            )
            save_last_recipe_save_folder_id(fid)
            show_toast(self, "配方已保存到「配方管理」", style="success")
        except (OSError, ValueError) as e:
            show_toast(self, f"保存失败：{e}", style="error")

    def _populate_simulation_results(
        self, rows: list[dict], recipe: dict
    ) -> None:
        snapshot = list(rows)
        if self._is_five_mode():
            self._simulation_rows_five = snapshot
            self._simulation_metrics_five = dict(recipe)
        else:
            self._simulation_rows_ten = snapshot
            self._simulation_metrics_ten = dict(recipe)
        self._rebuild_results_from_rows(snapshot)
        self._apply_results_summary(recipe)
        self._relayout_results_grid()
        self._update_save_recipe_button_state()

    def import_substrates_from_recipe_dict(self, recipe: dict) -> str | None:
        """
        从已保存配方（含 ``substrates_display``）填入当前模拟底物槽。
        仅支持 5 个或 10 个底物（五合一 / 十合一）；不拉取价格、不计算模拟结果。
        成功返回 None，失败返回简短错误文案。
        """
        if not isinstance(recipe, dict):
            return "配方数据无效"
        subs = recipe.get("substrates_display")
        if not isinstance(subs, list) or not subs:
            return "配方中没有底物数据，无法导入"
        k_meta = recipe.get("simulation_slot_count")
        if k_meta is not None:
            try:
                k_meta = int(k_meta)
            except (TypeError, ValueError):
                k_meta = None
        n = len(subs)
        if k_meta in (5, 10):
            if n != k_meta:
                return f"底物数量（{n}）与记录的模式（{k_meta} 合一）不一致"
            k = k_meta
        elif n == 5:
            k = 5
        elif n == 10:
            k = 10
        else:
            return "仅支持五合一（5 个底物）或十合一（10 个底物）的配方"

        want_five = k == 5
        if want_five != self._is_five_mode():
            if want_five:
                self._mode_five_btn.setChecked(True)
            else:
                self._mode_ten_btn.setChecked(True)
            self._mode_switch.sync_mode_slider(animate=False)
            self._apply_mode_to_cards()
            self._defocus_substrate_line_edits_if_any()

        n_vis = self._visible_slot_count()
        for i in range(n_vis):
            self._cards[i].reset_substrate()

        for i in range(k):
            s = subs[i]
            if not isinstance(s, dict):
                return "底物数据格式无效"
            raw_name = str(s.get("name") or "").strip()
            name = _strip_substrate_name_for_simulation_import(raw_name)
            if not name:
                return "底物缺少饰品名称"
            raw_fv = s.get("float_value")
            if raw_fv is None:
                return "底物缺少磨损"
            try:
                wear_f = float(raw_fv)
            except (TypeError, ValueError):
                return "底物磨损无效"
            wear_str = _substrate_float_value_display_str(raw_fv, parsed=wear_f)
            self._cards[i].restore_substrate_snapshot(name, wear_str)

        for i in range(k):
            self._cards[i].revalidate_search_selection()

        for i in range(k):
            c = self._cards[i]
            tpl = c.skin_template()
            nm = c.selected_display_name()
            if tpl is None or not nm:
                return f"第 {i + 1} 个底物无法匹配到皮肤库，请手动选择"
            wear = _parse_wear_value(c.wear_text())
            if wear is None or not _wear_in_template_range(tpl, wear):
                return f"第 {i + 1} 个底物磨损与皮肤不匹配，请检查"

        if want_five:
            self._simulation_rows_five = None
            self._simulation_metrics_five = None
            self._simulation_substrate_prices_five = None
            self._slot_snapshots_five = [
                self._snapshot_card(self._cards[i]) for i in range(5)
            ]
        else:
            self._simulation_rows_ten = None
            self._simulation_metrics_ten = None
            self._simulation_substrate_prices_ten = None
            self._slot_snapshots_ten = [
                self._snapshot_card(self._cards[i]) for i in range(_SIM_MAX_CARDS)
            ]

        self._clear_results_ui_only()
        self._results_section.hide()
        self._mode_switch.sync_mode_slider(animate=False)
        self._update_save_recipe_button_state()
        for i in range(n_vis):
            self._cards[i].refresh_clear_buttons()
        QTimer.singleShot(0, self._relayout_scroll_content)
        return None

    def current_mode_slot_limit(self) -> int:
        return self._visible_slot_count()

    def _ensure_mode_slot_count(self, slot_count: int) -> None:
        if slot_count not in (5, 10):
            return
        want_five = slot_count == 5
        if want_five != self._is_five_mode():
            if want_five:
                self._mode_five_btn.setChecked(True)
            else:
                self._mode_ten_btn.setChecked(True)
            self._mode_switch.sync_mode_slider(animate=False)
            self._apply_mode_to_cards()
            self._defocus_substrate_line_edits_if_any()

    def filled_substrate_count_for_slot_count(self, slot_count: int) -> int:
        self._ensure_mode_slot_count(slot_count)
        return self.current_filled_substrate_count()

    def current_filled_substrate_count(self) -> int:
        n = self._visible_slot_count()
        cnt = 0
        for i in range(n):
            c = self._cards[i]
            tpl = c.skin_template()
            nm = c.selected_display_name()
            wear = _parse_wear_value(c.wear_text())
            if tpl is not None and nm and wear is not None and _wear_in_template_range(tpl, wear):
                cnt += 1
        return cnt

    def import_inventory_items(self, items: list[dict], *, slot_count: int | None = None) -> str | None:
        if not isinstance(items, list) or not items:
            return "没有可导入的物品"
        if slot_count in (5, 10):
            self._ensure_mode_slot_count(int(slot_count))
        n = self._visible_slot_count()
        # 找到当前模式下空槽位：仅增量填充，不要求一次凑满 5/10。
        empty_indices: list[int] = []
        for i in range(n):
            c = self._cards[i]
            tpl = c.skin_template()
            wear = _parse_wear_value(c.wear_text())
            nm = c.selected_display_name()
            if tpl is None or not nm or wear is None or not _wear_in_template_range(tpl, wear):
                empty_indices.append(i)
        if len(items) > len(empty_indices):
            total_count = (n - len(empty_indices)) + len(items)
            return (
                f"底物数量过多（共 {total_count} 件），"
                "请清理「炼金模拟」中已添加物品或减少物品选择"
            )

        for slot_i, it in zip(empty_indices, items):
            if not isinstance(it, dict):
                return "导入数据格式无效"
            tpl = resolve_inventory_skin_template(it)
            if tpl is not None:
                weapon = (getattr(tpl, "weapon_name", "") or "").strip()
                skin = (getattr(tpl, "skin_name", "") or "").strip()
                raw_name = f"{weapon} | {skin}" if weapon and skin else (weapon or "")
            else:
                raw_name = (
                    (it.get("market_hash_name") or "").strip()
                    or (it.get("market_name") or "").strip()
                    or (it.get("name") or "").strip()
                )
            if not raw_name:
                return "存在缺少名称的库存物品"
            raw_fv = it.get("float")
            try:
                wear = float(raw_fv)
            except (TypeError, ValueError):
                return "存在无效磨损的库存物品"
            wear_str = _substrate_float_value_display_str(raw_fv, parsed=wear)
            self._cards[slot_i].restore_substrate_snapshot(raw_name, wear_str)
            self._cards[slot_i].revalidate_search_selection()
            c = self._cards[slot_i]
            tpl2 = c.skin_template()
            nm2 = c.selected_display_name()
            if tpl2 is None or not nm2:
                return f"第 {slot_i + 1} 个底物无法匹配到皮肤库，请手动选择"
            w2 = _parse_wear_value(c.wear_text())
            if w2 is None or not _wear_in_template_range(tpl2, w2):
                return f"第 {slot_i + 1} 个底物磨损与皮肤不匹配，请检查"

        if self._is_five_mode():
            self._slot_snapshots_five = [self._snapshot_card(self._cards[i]) for i in range(5)]
            self._simulation_rows_five = None
            self._simulation_metrics_five = None
            self._simulation_substrate_prices_five = None
        else:
            self._slot_snapshots_ten = [self._snapshot_card(self._cards[i]) for i in range(_SIM_MAX_CARDS)]
            self._simulation_rows_ten = None
            self._simulation_metrics_ten = None
            self._simulation_substrate_prices_ten = None
        self._clear_results_ui_only()
        self._results_section.hide()
        self._update_save_recipe_button_state()
        for i in range(n):
            self._cards[i].refresh_clear_buttons()
        QTimer.singleShot(0, self._relayout_scroll_content)
        return None

    def _on_clear_data_clicked(self) -> None:
        """清空当前模式下的底物槽与模拟结果，不影响另一模式。"""
        n = self._visible_slot_count()
        for i in range(n):
            self._cards[i].reset_substrate()
        if self._is_five_mode():
            self._slot_snapshots_five = [("", "") for _ in range(5)]
            self._simulation_rows_five = None
        else:
            self._slot_snapshots_ten = [("", "") for _ in range(_SIM_MAX_CARDS)]
            self._simulation_rows_ten = None
        self._simulation_metrics_five = None
        self._simulation_metrics_ten = None
        self._simulation_substrate_prices_five = None
        self._simulation_substrate_prices_ten = None
        self._clear_results_ui_only()
        self._results_section.hide()
        for i in range(n):
            self._cards[i].refresh_clear_buttons()
        self._update_save_recipe_button_state()
        QTimer.singleShot(0, self._relayout_scroll_content)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_page_title_icon()
        # 初次显示时强制 repolish 一次滚动条相关控件，避免某些平台上子滚动条首次仍沿用原生样式。
        QTimer.singleShot(0, self._refresh_scroll_area_styles)
        for card in self._cards:
            card.prepare_names_index()
        self._mode_switch.sync_mode_slider(animate=False)
        QTimer.singleShot(0, self._relayout_scroll_content)
        QTimer.singleShot(0, self._update_save_recipe_button_state)
        # 在他页切换主题时本页隐藏，changeEvent 因 isVisible() 为 False 不会刷新；
        # WeaponCardImageArea 用内联 qlineargradient（palette 色在写入时已固定），须在每次显示时重算。
        QTimer.singleShot(0, self._refresh_simulation_weapon_image_palettes)
        QTimer.singleShot(0, self._refresh_results_header_for_palette)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._relayout_scroll_content)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        # 仅响应 PaletteChange：模拟完成时增删产物区会触发 StyleChange，若此时刷新全部底物卡，
        # 隐藏槽位（另一模式）的图区也会重算渐变，表现为「五/十合一互相牵连」。
        if event.type() == QEvent.Type.PaletteChange:
            if self.isVisible():
                QTimer.singleShot(0, self._apply_page_title_icon)
                QTimer.singleShot(0, self._refresh_scroll_area_styles)
                QTimer.singleShot(0, self._refresh_results_header_for_palette)
                QTimer.singleShot(0, self._refresh_simulation_weapon_image_palettes)

    def _refresh_scroll_area_styles(self) -> None:
        widgets = (
            self._scroll,
            self._scroll.viewport(),
            self._scroll.verticalScrollBar(),
            self._results_header_scroll,
            self._results_header_scroll.viewport(),
            self._results_header_scroll.horizontalScrollBar(),
        )
        for widget in widgets:
            if widget is None:
                continue
            style = widget.style()
            if style is None:
                continue
            style.unpolish(widget)
            style.polish(widget)
            widget.update()

    def _refresh_simulation_weapon_image_palettes(self) -> None:
        for c in self._cards:
            c.refresh_weapon_image_palette_if_any()
        for c in self._result_cards:
            c.refresh_weapon_image_palette()

    def _apply_page_title_icon(self) -> None:
        lb = self._page_title_icon_label
        if not ALCHEMY_SIMULATION_ICON_PATH.is_file():
            lb.clear()
            return
        self.ensurePolished()
        color = self.palette().color(QPalette.ColorRole.WindowText).name()
        px = 28
        ico = load_svg_icon(ALCHEMY_SIMULATION_ICON_PATH, color, size=px)
        pm = ico.pixmap(px, px)
        if pm is not None and not pm.isNull():
            lb.setPixmap(pm)

    def _on_simulation_price_fetch_finished(self, price_map: object, error_msg: str | None) -> None:
        self._simulation_price_fetch_busy = False
        self._fetch_worker = None
        substrates = self._pending_simulation_substrates
        self._pending_simulation_substrates = None

        if error_msg == "__cancelled__":
            self._simulation_set_calc_idle()
            return
        if error_msg:
            self._simulation_set_calc_idle()
            show_alert(self, "加载产物价格失败", str(error_msg))
            return
        if not isinstance(price_map, dict) or substrates is None:
            self._simulation_set_calc_idle()
            return

        err, rows, avg_nfv = compute_tradeup_simulation_products(substrates)
        if err:
            self._simulation_set_calc_idle()
            show_toast(self, err, style="warning")
            return
        if not rows or avg_nfv is None:
            self._simulation_set_calc_idle()
            show_toast(self, "无法生成产物分布", style="warning")
            return

        sub_prices, recipe = apply_simulation_prices_and_recipe_metrics(
            substrates, rows, price_map, avg_nfv
        )

        if self._is_five_mode():
            self._simulation_substrate_prices_five = list(sub_prices)
        else:
            self._simulation_substrate_prices_ten = list(sub_prices)

        n = len(substrates)
        for i in range(n):
            self._cards[i].set_substrate_price_yuan(sub_prices[i])
        for i in range(n, _SIM_MAX_CARDS):
            self._cards[i].set_substrate_price_yuan(None)

        self._populate_simulation_results(rows, recipe)
        self._simulation_set_calc_idle()

    def _on_start_calculate_clicked(self) -> None:
        substrate_rows, err = self._validated_visible_substrates()
        if err:
            show_toast(self, err, style="warning")
            return
        substrates = [(tpl, wear) for tpl, wear, _ in substrate_rows]

        self._pending_simulation_substrates = substrates
        self._simulation_price_fetch_busy = True
        self._simulation_set_calc_busy()
        show_toast(self, "正在加载产物价格...", style="info")

        self._fetch_worker = FetchPriceWorker(self)
        self._fetch_worker.finished.connect(self._on_simulation_price_fetch_finished)
        self._fetch_worker.start()
