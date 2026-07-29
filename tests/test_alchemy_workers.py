from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from ui.workers.alchemy_workers import (
    _CalcPoolThread,
    _build_non_overlapping_group_recipes,
)


def _rows(count: int) -> list[dict]:
    return [
        {
            "goods_name": "AK-47 | 传承（略有磨损）",
            "float_value": 0.08 + index / 1_000_000,
            "price": 10 + index,
            "platform": "buff",
        }
        for index in range(count)
    ]


def _recipe(rows: list[dict], expectation: float) -> dict:
    return {
        "solution": [],
        "expectation": expectation,
        "rate": 0.2,
        "cost": sum(float(row["price"]) for row in rows),
        "substrates_display": [
            {
                "name": row["goods_name"],
                "float_value": row["float_value"],
                "price": row["price"],
                "platform": row["platform"],
            }
            for row in rows
        ],
        "products_display": [],
    }


def _overlapping_recipes(rows: list[dict]) -> list[dict]:
    return [
        _recipe(rows[:10], 100.0),
        _recipe(rows[:10], 90.0),
    ]


class AlchemyWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def _run_target(self, non_overlapping_recipes: bool) -> list[dict]:
        emitted: list[tuple[list[dict], object]] = []
        rows = _rows(10)
        worker = _CalcPoolThread(
            selected_data=rows,
            price_map={},
            norm_min=0.5,
            norm_max=0.5,
            mode="target",
            non_overlapping_recipes=non_overlapping_recipes,
        )
        worker.task_finished.connect(
            lambda recipes, error: emitted.append((recipes, error))
        )
        with (
            patch(
                "ui.workers.alchemy_workers.partition_selected_data_by_tradeup_group",
                return_value=[("军规级", False, 10, rows)],
            ),
            patch(
                "ui.workers.alchemy_workers.compute_recipes",
                return_value=(copy.deepcopy(_overlapping_recipes(rows)), None),
            ),
        ):
            worker.run()
        self.assertEqual(len(emitted), 1)
        self.assertIsNone(emitted[0][1])
        return emitted[0][0]

    def test_non_overlapping_switch_controls_result_filter(self) -> None:
        self.assertEqual(len(self._run_target(non_overlapping_recipes=True)), 1)
        self.assertEqual(len(self._run_target(non_overlapping_recipes=False)), 2)

    def test_one_hundred_military_items_make_ten_disjoint_recipes(self) -> None:
        rows = _rows(100)

        def solve(remaining, *_args, **_kwargs):
            return ([_recipe(list(remaining)[:10], 1000 - len(remaining))], None)

        with patch(
            "ui.workers.alchemy_workers.compute_recipes",
            side_effect=solve,
        ):
            recipes = _build_non_overlapping_group_recipes(
                rows=rows,
                initial_recipes=[_recipe(rows[:10], 900.0)],
                k=10,
                price_map={},
                norm_min=0.5,
                norm_max=0.5,
                mode="target",
                timeout=30,
                min_break_even_rate=0,
                cancel_check=lambda: False,
            )

        self.assertEqual(len(recipes), 10)
        used = [
            substrate["float_value"]
            for recipe in recipes
            for substrate in recipe["substrates_display"]
        ]
        self.assertEqual(len(used), 100)
        self.assertEqual(len(set(used)), 100)


if __name__ == "__main__":
    unittest.main()
