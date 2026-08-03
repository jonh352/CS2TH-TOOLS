from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.widgets.collapsible_group import CollapsibleGroup
from ui.workers.alchemy_workers import _scan_mode_process_pool_workers


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _items(count: int) -> list[dict]:
    return [
        {
            "goods_name": "AK-47 | 夜愿（略有磨损）",
            "float_value": 0.1 + index * 0.000001,
            "platform": "buff",
            "price": 10.0,
            "goods_id": str(index),
        }
        for index in range(count)
    ]


def test_collapsed_group_keeps_rows_lazy_and_selection_state() -> None:
    _app()
    group = CollapsibleGroup("AK-47 | 夜愿（略有磨损）", _items(1000))

    assert group.table_widget.rowCount() == 0
    assert len(group.get_selected_items()) == 1000

    slot_key = next(iter(group.get_all_slot_keys()))
    assert group.set_row_state_by_slot_key(
        slot_key, calc_checked=False, must_checked=False
    )
    assert len(group.get_selected_items()) == 999

    group.toggle()
    assert group.table_widget.rowCount() == 1000
    assert len(group.get_selected_items()) == 999


def test_large_input_reduces_process_fanout_without_disabling_parallelism() -> None:
    normal = _scan_mode_process_pool_workers(task_count=100, input_count=100)
    large = _scan_mode_process_pool_workers(task_count=100, input_count=1000)
    very_large = _scan_mode_process_pool_workers(task_count=100, input_count=3000)

    assert normal >= large >= very_large >= 1
    assert large <= 4
    assert very_large <= 2
