"""Compact special-float lookup page."""

from __future__ import annotations

import json

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import CONTENT_PAGE_LAYOUT_MARGINS, META_DIR
from core.alchemy_quality import get_name_map, get_pid_map, normalize_name
from core.alchemy_calc import get_k_from_quality
from core.data_utils import SkinTemplate
from core.float32_wear_prefix import find_float32_range_intersection
from core.special_wear_materials import build_special_wear_materials
from core.special_wear_names import get_skin_full_names_without_appearance
from ui.components import PageHeader, panel


def _display_name(template: SkinTemplate) -> str:
    return (
        f"{template.weapon_name} | {template.skin_name}"
        if template.skin_name
        else template.weapon_name
    )


def _format_float(value: float) -> str:
    return f"{value:.18f}".rstrip("0").rstrip(".")


def _common_values() -> tuple[list[str], list[str]]:
    try:
        raw = json.loads((META_DIR / "commonly_used.json").read_text(encoding="utf-8"))
        products = [str(x).strip() for x in raw.get("product", []) if str(x).strip()]
        floats = [str(x).strip() for x in raw.get("float", []) if str(x).strip()]
        return products[:12], floats
    except Exception:
        return [], []


class SpecialWearPage(QWidget):
    materials_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._material_payload: dict = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        root.setSpacing(18)
        root.addWidget(
            PageHeader(
                "Float Lab",
                "特殊磨损查询",
                "按 CS2 的 float32 精度计算目标产物范围，并反推每种可用底物的对应磨损。",
            )
        )

        form_frame, form = panel(self)
        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(8)
        fields.addWidget(QLabel("目标皮肤"), 0, 0)
        fields.addWidget(QLabel("目标磨损"), 0, 1)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("输入并选择完整皮肤名称")
        self.name_edit.setClearButtonEnabled(True)
        self.wear_edit = QLineEdit()
        self.wear_edit.setPlaceholderText("例如 0.1314520")
        self.wear_edit.setClearButtonEnabled(True)
        self.query_button = QPushButton("查询")
        self.query_button.setObjectName("primaryButton")
        self.clear_button = QPushButton("清空")
        fields.addWidget(self.name_edit, 1, 0)
        fields.addWidget(self.wear_edit, 1, 1)
        fields.addWidget(self.query_button, 1, 2)
        fields.addWidget(self.clear_button, 1, 3)
        fields.setColumnStretch(0, 4)
        fields.setColumnStretch(1, 2)
        form.addLayout(fields)

        common_products, common_floats = _common_values()
        if common_products:
            common_label = QLabel("常用目标")
            common_label.setObjectName("muted")
            form.addWidget(common_label)
            chips = QGridLayout()
            chips.setSpacing(6)
            for index, name in enumerate(common_products):
                button = QPushButton(name)
                button.setToolTip(name)
                button.clicked.connect(lambda _=False, value=name: self.name_edit.setText(value))
                chips.addWidget(button, index // 4, index % 4)
            form.addLayout(chips)
        if common_floats:
            row = QHBoxLayout()
            label = QLabel("常用磨损")
            label.setObjectName("muted")
            row.addWidget(label)
            for value in common_floats:
                button = QPushButton(value)
                button.clicked.connect(lambda _=False, text=value: self.wear_edit.setText(text))
                row.addWidget(button)
            row.addStretch(1)
            form.addLayout(row)
        root.addWidget(form_frame)

        result_frame, result = panel(self)
        result_header = QHBoxLayout()
        self.range_label = QLabel("输入目标后显示 float32 可达区间")
        self.range_label.setObjectName("muted")
        self.range_label.setWordWrap(True)
        self.collect_button = QPushButton("前往材料采集")
        self.collect_button.setObjectName("primaryButton")
        self.collect_button.hide()
        self.collect_button.clicked.connect(
            lambda: self.materials_requested.emit(self._material_payload)
        )
        result_header.addWidget(self.range_label, 1)
        result_header.addWidget(self.collect_button)
        result.addLayout(result_header)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("可用底物", "对应磨损", "可采购区间"))
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        result.addWidget(self.table, 1)
        root.addWidget(result_frame, 1)

        names = get_skin_full_names_without_appearance()
        completer = QCompleter(QStringListModel(names, self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setMaxVisibleItems(12)
        self.name_edit.setCompleter(completer)

        self.query_button.clicked.connect(self.query)
        self.clear_button.clicked.connect(self.clear)
        self.wear_edit.returnPressed.connect(self.query)

    def _template(self) -> SkinTemplate | None:
        return get_name_map().get(normalize_name(self.name_edit.text()))

    def query(self) -> None:
        self._material_payload = {}
        self.collect_button.hide()
        template = self._template()
        if template is None:
            self.range_label.setText("请从候选列表中选择一个完整皮肤名称")
            self.table.setRowCount(0)
            return
        raw = self.wear_edit.text().strip().replace("。", ".").replace("．", ".")
        try:
            target = float(raw)
        except ValueError:
            self.range_label.setText("请输入有效磨损数字")
            self.table.setRowCount(0)
            return
        if not template.min_float <= target <= template.max_float:
            self.range_label.setText(
                f"目标磨损需在 {_format_float(template.min_float)} ～ "
                f"{_format_float(template.max_float)} 之间"
            )
            self.table.setRowCount(0)
            return
        if not template.lower_skins:
            self.range_label.setText("该饰品无法通过汰换合同获得")
            self.table.setRowCount(0)
            return

        low, high, error = find_float32_range_intersection(
            raw, template.min_float, template.max_float
        )
        if error:
            self.range_label.setText(error)
            self.table.setRowCount(0)
            return
        assert low is not None and high is not None
        self.range_label.setText(
            "符合要求的产物磨损范围："
            f"{_format_float(low)} ～ {_format_float(high)}"
        )

        pid_map = get_pid_map()
        rows = build_special_wear_materials(
            template,
            target=target,
            target_low=low,
            target_high=high,
            pid_map=pid_map,
        )
        self.table.setRowCount(len(rows))
        for row, material in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(material["name"]))
            self.table.setItem(
                row, 1, QTableWidgetItem(_format_float(material["wear_value"]))
            )
            self.table.setItem(
                row,
                2,
                QTableWidgetItem(
                    f"{_format_float(material['min_wear'])} ～ "
                    f"{_format_float(material['max_wear'])}"
                ),
            )
        self.table.resizeRowsToContents()
        input_quality = ""
        for pid in template.lower_skins:
            lower_template = pid_map.get(str(pid))
            if lower_template is not None:
                input_quality = str(lower_template.quality or "")
                break
        self._material_payload = {
            "source": "special_wear",
            "target_name": _display_name(template),
            "target_paint_index": str(template.paint_index),
            "target_input": raw,
            "target_min_wear": low,
            "target_max_wear": high,
            "input_quality": input_quality,
            "slot_count": get_k_from_quality(input_quality),
            "materials": rows,
        }
        self.collect_button.setVisible(bool(rows))

    def clear(self) -> None:
        self.name_edit.clear()
        self.wear_edit.clear()
        self.range_label.setText("输入目标后显示 float32 可达区间")
        self.table.setRowCount(0)
        self._material_payload = {}
        self.collect_button.hide()
