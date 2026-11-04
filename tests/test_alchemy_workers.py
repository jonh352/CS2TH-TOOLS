from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from ui.workers.alchemy_workers import (
    _CalcPoolThread,
    _build_non_overlapping_group_recipes,
    SpecialWearCalcWorker,
)
from ui.pages.alchemy_modes import AlchemyModeMixin


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

    def _run_target(self) -> list[dict]:
        emitted: list[tuple[list[dict], object]] = []
        rows = _rows(10)
        worker = _CalcPoolThread(
            selected_data=rows,
            price_map={},
            norm_min=0.5,
            norm_max=0.5,
            mode="target",
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

    def test_target_returns_overlapping_recipes_without_resolving(self) -> None:
        self.assertEqual(len(self._run_target()), 2)

    def test_non_overlapping_hundred_items_make_ten_disjoint_recipes(self) -> None:
        rows = _rows(100)
        with patch("ui.workers.alchemy_workers.compute_recipes",
                   side_effect=lambda remaining, *_a, **_k: ([_recipe(remaining[:10], 100)], None)):
            recipes = _build_non_overlapping_group_recipes(
                rows=rows, initial_recipes=[_recipe(rows[:10], 100)], k=10,
                price_map={}, norm_min=0, norm_max=1, mode="target", timeout=30,
                min_break_even_rate=0, max_break_even_rate=1, cancel_check=lambda: False,
            )
        used = [s["float_value"] for r in recipes for s in r["substrates_display"]]
        self.assertEqual(len(recipes), 10)
        self.assertEqual(len(set(used)), 100)

    def test_non_overlapping_keeps_profitable_recipe_before_expensive_loss(self) -> None:
        from core.alchemy_calc import filter_non_overlapping_recipes
        rows = _rows(20)
        profitable = _recipe(rows[:10], 564.80)
        profitable.update(cost=523.38, rate=564.80 / 523.38 - 1, break_even_rate=0.2)
        expensive = _recipe(rows[:5] + rows[10:15], 959.74)
        expensive.update(cost=1269.85, rate=959.74 / 1269.85 - 1, break_even_rate=0.1)
        with patch("ui.workers.alchemy_workers.compute_recipes", return_value=([], None)):
            selected = _build_non_overlapping_group_recipes(
                rows=rows, initial_recipes=[expensive, profitable], k=10,
                price_map={}, norm_min=0, norm_max=1, mode="target", timeout=30,
                min_break_even_rate=0, max_break_even_rate=1, cancel_check=lambda: False,
            )
        self.assertEqual(selected, [profitable])
        self.assertEqual(filter_non_overlapping_recipes([expensive, profitable]), [profitable])

    def test_non_overlapping_selection_follows_chosen_sort_mode(self) -> None:
        from core.alchemy_calc import best_ranked_recipe
        rows = _rows(10)
        higher_rate = dict(_recipe(rows, 100), rate=0.2, break_even_rate=0.3)
        higher_break_even = dict(_recipe(rows, 200), rate=0.1, break_even_rate=0.8)
        self.assertIs(best_ranked_recipe([higher_break_even, higher_rate]), higher_rate)
        self.assertIs(best_ranked_recipe([higher_break_even, higher_rate], "break_even_rate"), higher_break_even)

    def test_non_overlapping_workers_accept_more_than_hundred_items(self) -> None:
        rows = _rows(101)
        worker = _CalcPoolThread(rows, {}, 0, 1, "scan", non_overlapping_recipes=True)
        with patch("ui.workers.alchemy_workers.partition_selected_data_by_tradeup_group", return_value=[]) as partition:
            worker.run()
        partition.assert_called_once()
        worker = SpecialWearCalcWorker(rows, {}, "1", 0, 1, non_overlapping_recipes=True)
        with patch("ui.workers.alchemy_workers.eligible_selected_data_for_target", return_value=[]) as eligible:
            worker.run()
        eligible.assert_called_once()

    def test_target_worker_forwards_break_even_range(self) -> None:
        rows = _rows(10)
        worker = _CalcPoolThread(
            selected_data=rows,
            price_map={},
            norm_min=0.5,
            norm_max=0.5,
            mode="target",
            min_break_even_rate=0.25,
            max_break_even_rate=0.75,
        )
        with (
            patch(
                "ui.workers.alchemy_workers.partition_selected_data_by_tradeup_group",
                return_value=[("军规级", False, 10, rows)],
            ),
            patch(
                "ui.workers.alchemy_workers.compute_recipes",
                return_value=([], None),
            ) as compute,
        ):
            worker.run()

        self.assertEqual(compute.call_args.kwargs["min_break_even_rate"], 0.25)
        self.assertEqual(compute.call_args.kwargs["max_break_even_rate"], 0.75)

    def test_running_progress_never_claims_one_hundred_percent(self) -> None:
        class Sink:
            def __init__(self) -> None:
                self.value = None

            def setValue(self, value: int) -> None:
                self.value = value

            def setText(self, value: str) -> None:
                self.value = value

        page = type("ProgressPage", (), {})()
        page.step3_progress_bar = Sink()
        page.step3_progress_label = Sink()
        AlchemyModeMixin._on_calc_progress(page, 100)
        self.assertEqual(page.step3_progress_bar.value, 99)
        self.assertEqual(page.step3_progress_label.value, "99%")



if __name__ == "__main__":
    unittest.main()
