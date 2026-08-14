from __future__ import annotations

import unittest
import time
from contextlib import nullcontext
from threading import Lock
from unittest.mock import patch

from core.collection_cancel import CollectionCancelled, interruptible_wait
from ui.workers.material_collection import (
    MaterialCollectionWorker,
    dedupe_candidates_keep_cheapest,
)
from ui.workers.special_collection import SpecialCollectionWorker


class CollectionCancellationTests(unittest.TestCase):
    def test_special_worker_groups_candidates_with_legacy_solver(self) -> None:
        candidates = [
            {
                "platform": "buff",
                "listing_id": str(index),
                "goods_id": str(index),
                "goods_name": "skin-a",
                "float_value": 0.1 + index / 1000,
                "price": float(index + 1),
            }
            for index in range(5)
        ]
        recipe = {"cost": 15.0, "substrates_display": candidates}
        worker = SpecialCollectionWorker(
            materials=[{"name": "skin-a", "min_wear": 0.1, "max_wear": 0.2}],
            providers=["buff"],
            provider_intervals={"buff": 5},
            target_paint_index="321",
            target_wear_low=0.131,
            target_wear_high=0.132,
            slot_count=5,
        )
        emitted: list[tuple] = []
        units: list[tuple[int, int]] = []
        worker.completed.connect(
            lambda rows, recipes, message: emitted.append((rows, recipes, message))
        )
        worker.progress_units.connect(lambda done, total: units.append((done, total)))

        def fake_solve(rows, price_map, paint_index, low, high, **kwargs):
            self.assertEqual(rows, candidates)
            self.assertEqual(price_map, {})
            self.assertEqual(paint_index, "321")
            self.assertEqual((low, high), (0.131, 0.132))
            self.assertEqual(kwargs["rounds"], 3)
            kwargs["progress_callback"](0, 1, 0, 67, 1, 3, 5, 5)
            return [recipe], None

        with patch(
            "ui.workers.special_collection.collect_candidates_parallel",
            return_value=(candidates, [], {}),
        ), patch(
            "ui.workers.special_collection.compute_special_wear_recipes",
            side_effect=fake_solve,
        ):
            worker.run()

        self.assertEqual(emitted, [(candidates, [recipe], "")])
        self.assertIn((0, 100), units)
        self.assertIn((67, 100), units)
        self.assertEqual(units[-1], (100, 100))

    def test_interruptible_wait_stops_immediately(self) -> None:
        with self.assertRaises(CollectionCancelled):
            interruptible_wait(30, lambda: True)

    def test_material_worker_emits_partial_rows_when_stopped(self) -> None:
        worker = MaterialCollectionWorker(
            materials=[
                {"name": "skin-a", "min_wear": 0.1, "max_wear": 0.2},
                {"name": "skin-b", "min_wear": 0.1, "max_wear": 0.2},
            ],
            providers=["buff"],
            provider_intervals={"buff": 5},
        )
        emitted: list[tuple] = []
        worker.completed.connect(
            lambda rows, message, retry: emitted.append((rows, message, retry))
        )
        calls = {"n": 0}

        def fake_fetch(_provider: str, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    {
                        "platform": "buff",
                        "listing_id": "kept-1",
                        "goods_id": "kept-1",
                        "goods_name": "skin-a",
                        "float_value": 0.15,
                        "price": 1.2,
                    }
                ]
            worker.request_stop()
            raise CollectionCancelled("stopped")

        with (
            patch(
                "ui.workers.material_collection.get_name_map",
                return_value={"skin-a": object(), "skin-b": object()},
            ),
            patch(
                "ui.workers.material_collection.normalize_name",
                side_effect=lambda name: name,
            ),
            patch(
                "ui.workers.material_collection.fetch_exact_wear_candidates",
                side_effect=fake_fetch,
            ),
            patch("ui.workers.material_collection.close_access_sessions"),
            patch(
                "ui.workers.material_collection.interruptible_wait",
            ),
        ):
            worker.run()

        self.assertEqual(len(emitted), 1)
        rows, message, _retry = emitted[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["listing_id"], "kept-1")
        self.assertIn("已停止", message)

    def test_material_worker_emits_progress_units(self) -> None:
        worker = MaterialCollectionWorker(
            materials=[
                {"name": "skin-a", "min_wear": 0.1, "max_wear": 0.2},
                {"name": "skin-b", "min_wear": 0.1, "max_wear": 0.2},
            ],
            providers=["buff"],
            provider_intervals={"buff": 5},
        )
        units: list[tuple[int, int]] = []
        from PySide6.QtCore import Qt as QtCoreQt

        worker.progress_units.connect(
            lambda done, total: units.append((done, total)),
            QtCoreQt.ConnectionType.DirectConnection,
        )

        def fake_fetch(_provider: str, **_kwargs):
            return [
                {
                    "platform": "buff",
                    "listing_id": "x",
                    "goods_id": "x",
                    "goods_name": "skin-a",
                    "float_value": 0.15,
                    "price": 1.0,
                }
            ]

        with (
            patch(
                "ui.workers.material_collection.get_name_map",
                return_value={"skin-a": object(), "skin-b": object()},
            ),
            patch(
                "ui.workers.material_collection.normalize_name",
                side_effect=lambda name: name,
            ),
            patch(
                "ui.workers.material_collection.fetch_exact_wear_candidates",
                side_effect=fake_fetch,
            ),
            patch("ui.workers.material_collection.close_access_sessions"),
            patch("ui.workers.material_collection.interruptible_wait"),
        ):
            worker.run()

        self.assertEqual(units[0], (0, 2))
        self.assertEqual(units[-1], (2, 2))
        self.assertIn((1, 2), units)

    def test_material_worker_collects_in_buff_yyyp_then_c5_eco_waves(self) -> None:
        worker = MaterialCollectionWorker(
            materials=[{"name": "skin", "min_wear": 0.1, "max_wear": 0.2}],
            providers=["buff", "yyyp", "c5", "eco"],
            provider_intervals={key: 5 for key in ("buff", "yyyp", "c5", "eco")},
        )
        active = 0
        max_active = 0
        started: dict[str, float] = {}
        ended: dict[str, float] = {}
        lock = Lock()
        page_limits: list[int] = []

        def fake_fetch(provider: str, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                started[provider] = time.monotonic()
                page_limits.append(kwargs["max_pages"])
            time.sleep(0.08)
            with lock:
                active -= 1
                ended[provider] = time.monotonic()
            return [
                {
                    "platform": provider,
                    "listing_id": f"{provider}-1",
                    "goods_id": f"{provider}-1",
                }
            ]

        with (
            patch("ui.workers.material_collection.get_name_map", return_value={"skin": object()}),
            patch("ui.workers.material_collection.normalize_name", return_value="skin"),
            patch(
                "ui.workers.material_collection.fetch_exact_wear_candidates",
                side_effect=fake_fetch,
            ),
            patch("ui.workers.material_collection.close_access_sessions"),
            patch(
                "ui.workers.material_collection.c5_signer_collection_scope",
                return_value=nullcontext(),
            ),
        ):
            worker.run()

        self.assertEqual(max_active, 2)
        self.assertEqual(page_limits, [0, 0, 0, 0])
        wave1_done = max(ended["buff"], ended["yyyp"])
        self.assertGreaterEqual(started["c5"], wave1_done)
        self.assertGreaterEqual(started["eco"], wave1_done)

    def test_provider_collection_waves_order(self) -> None:
        from ui.workers.material_collection import _provider_collection_waves

        self.assertEqual(
            _provider_collection_waves(["eco", "buff", "c5", "yyyp"]),
            [["buff", "yyyp"], ["eco", "c5"]],
        )
        self.assertEqual(_provider_collection_waves(["c5"]), [["c5"]])
        self.assertEqual(_provider_collection_waves(["buff", "yyyp"]), [["buff", "yyyp"]])

    def test_dedupe_candidates_keep_cheapest_across_platforms(self) -> None:
        rows = [
            {
                "goods_name": "P90 | 风蚀流光",
                "float_value": 0.079459,
                "price": 3.5,
                "platform": "buff",
                "listing_id": "b1",
            },
            {
                "goods_name": "P90 | 风蚀流光",
                "float_value": 0.079459,
                "price": 2.8,
                "platform": "eco",
                "listing_id": "e1",
            },
            {
                "goods_name": "P90 | 风蚀流光",
                "float_value": 0.079459,
                "price": 2.9,
                "platform": "yyyp",
                "listing_id": "y1",
            },
            {
                "goods_name": "P90 | 风蚀流光",
                "float_value": 0.08,
                "price": 4.0,
                "platform": "buff",
                "listing_id": "b2",
            },
            {
                "goods_name": "P90 | 风蚀流光",
                "float_value": 0.079459,
                "price": 2.8,
                "platform": "eco",
                "listing_id": "e1",
            },
        ]
        out = dedupe_candidates_keep_cheapest(rows)
        self.assertEqual(len(out), 2)
        by_wear = {round(float(r["float_value"]), 8): r for r in out}
        self.assertEqual(by_wear[0.079459]["platform"], "eco")
        self.assertEqual(by_wear[0.079459]["price"], 2.8)
        self.assertEqual(by_wear[0.08]["platform"], "buff")


if __name__ == "__main__":
    unittest.main()
