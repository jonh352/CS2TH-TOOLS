"""Dialog for adding synthetic trade-up substrates from local metadata."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QListView,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.alchemy_quality import get_name_map, normalize_name
from core.data_utils import SkinTemplate
from core.weapon_box_catalog import get_weapon_box_pick_rows
from ui.widgets.wear_interval_bar import WearRangeSelector


_QUALITY_ORDER = {
    "消费级": 0,
    "工业级": 1,
    "军规级": 2,
    "受限": 3,
    "保密": 4,
    "隐秘": 5,
    "非凡": 6,
}


@lru_cache(maxsize=1)
def custom_item_catalog() -> dict[str, list[SkinTemplate]]:
    """Return collection/weapon-box names mapped to their unique skin templates."""
    name_map = get_name_map()
    grouped: dict[str, dict[str, SkinTemplate]] = defaultdict(dict)
    for box_name, _quality, _stat_trak, names in get_weapon_box_pick_rows():
        for name in names:
            template = name_map.get(normalize_name(name))
            if template is not None and template.upper_skins:
                grouped[box_name][
                    f"{template.paint_index}:{int(bool(template.stat_trak))}"
                ] = template
    return {
        box_name: sorted(
            templates.values(),
            key=lambda item: (
                _QUALITY_ORDER.get(item.quality, 99),
                bool(item.stat_trak),
                item.weapon_name,
                item.skin_name,
            ),
        )
        for box_name, templates in sorted(grouped.items())
        if templates
    }


class CustomAlchemyItemDialog(QDialog):
    """Choose a collection, skin, wear interval and number of generated items."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加自定义饰品")
        self.setObjectName("customAlchemyItemDialog")
        self.resize(760, 500)
        self.setMinimumSize(640, 430)
        self._catalog = custom_item_catalog()

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        intro = QLabel(
            "先选择收藏品 / 武器箱及饰品，再拖动左右手柄确定磨损区间。"
            "添加多件时会在该区间内均匀生成磨损值。"
        )
        intro.setObjectName("alchemyStep1Hint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.box_combo = QComboBox()
        self.box_combo.addItems(list(self._catalog))
        self._configure_search_combo(self.box_combo)
        self.box_combo.currentTextChanged.connect(self._reload_skins)
        form.addRow("收藏品 / 武器箱", self.box_combo)

        self.skin_combo = QComboBox()
        self._configure_search_combo(self.skin_combo)
        self.skin_combo.currentIndexChanged.connect(self._sync_wear_bounds)
        form.addRow("饰品", self.skin_combo)

        self.quantity_spin = QSpinBox()
        self.quantity_spin.setObjectName("customItemQuantitySpin")
        self.quantity_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.quantity_spin.setRange(1, 200)
        self.quantity_spin.setValue(10)
        self.quantity_spin.setSuffix(" 件")
        self.quantity_spin.setToolTip("将在所选磨损区间内均匀生成对应数量的饰品")
        form.addRow("添加数量", self.quantity_spin)

        self.price_spin = QDoubleSpinBox()
        self.price_spin.setRange(0.0, 1_000_000.0)
        self.price_spin.setDecimals(2)
        self.price_spin.setPrefix("￥")
        self.price_spin.setSpecialValueText("自动匹配价格包")
        self.price_spin.setToolTip("保持为 0 时使用本地最新价格包；加入后仍可在列表中手动修改")
        form.addRow("单价", self.price_spin)
        root.addLayout(form)

        wear_title = QLabel("磨损区间（拖动左右手柄）")
        wear_title.setObjectName("sectionTitle")
        root.addWidget(wear_title)
        self.wear_selector = WearRangeSelector()
        root.addWidget(self.wear_selector)

        self.range_label = QLabel()
        self.range_label.setObjectName("recipeBridgeWear")
        self.range_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.range_label)
        self.wear_selector.rangeChanged.connect(self._sync_range_label)
        root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._reload_skins(self.box_combo.currentText())

    @staticmethod
    def _configure_search_combo(combo: QComboBox) -> None:
        """Use a compact, scrollable popup with contains-style search."""
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMaxVisibleItems(11)
        view = QListView(combo)
        view.setMaximumHeight(300)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        combo.setView(view)
        completer = QCompleter(combo.model(), combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.popup().setMaximumHeight(300)
        combo.setCompleter(completer)

    @staticmethod
    def _template_label(template: SkinTemplate) -> str:
        base = (
            f"{template.weapon_name} | {template.skin_name}"
            if template.skin_name
            else template.weapon_name
        )
        stat = " · StatTrak™" if template.stat_trak else ""
        return f"{base} · {template.quality}{stat}"

    def _reload_skins(self, box_name: str) -> None:
        # Typing a partial fuzzy-search term must not empty the current skin list.
        if str(box_name) not in self._catalog:
            return
        self.skin_combo.blockSignals(True)
        self.skin_combo.clear()
        for template in self._catalog.get(str(box_name), []):
            self.skin_combo.addItem(self._template_label(template), template)
        self.skin_combo.blockSignals(False)
        self._sync_wear_bounds()

    def _sync_wear_bounds(self, _index: int = -1) -> None:
        template = self.selected_template()
        if template is None:
            self.wear_selector.setEnabled(False)
            self.range_label.setText("当前收藏品没有可用饰品")
            return
        self.wear_selector.setEnabled(True)
        self.wear_selector.set_wear_bounds(template.min_float, template.max_float)
        self._sync_range_label(*self.wear_selector.selected_range())

    def _sync_range_label(self, low: float, high: float) -> None:
        self.range_label.setText(f"将生成 {low:g} ～ {high:g} 区间内的饰品")

    def selected_template(self) -> SkinTemplate | None:
        value = self.skin_combo.currentData()
        return value if isinstance(value, SkinTemplate) else None

    def selected_weapon_box_id(self) -> int:
        template = self.selected_template()
        box_name = self.box_combo.currentText().strip()
        if template is None:
            return 0
        for index, name in enumerate(template.weapon_box_name):
            if str(name).strip() == box_name and index < len(template.weapon_box_id):
                return int(template.weapon_box_id[index])
        return int(template.weapon_box_id[0]) if template.weapon_box_id else 0

    def selection(self) -> tuple[SkinTemplate, int, float, float, int, float] | None:
        template = self.selected_template()
        if template is None:
            return None
        low, high = self.wear_selector.selected_range()
        return (
            template,
            self.selected_weapon_box_id(),
            float(low),
            float(high),
            int(self.quantity_spin.value()),
            float(self.price_spin.value()),
        )
