from __future__ import annotations

import unittest
import time
from threading import Lock
from unittest.mock import patch

from core.collection_cancel import CollectionCancelled, interruptible_wait
from ui.workers.material_collection import MaterialCollectionWorker


class CollectionCancellationTests(unittest.TestCase):
    def test_interruptible_wait_stops_immediately(self) -> None:
        with self.assertRaises(CollectionCancelled):
            interruptible_wait(30, lambda: True)

    def test_material_worker_passes_live_cancel_callback(self) -> None:
        worker = MaterialCollectionWorker(
            materials=[{"name": "AK-47 | 传承", "min_wear": 0.1, "max_wear": 0.2}],
            providers=["buff"],
            provider_intervals={"buff": 5},
        )
        callback_seen: list[bool] = []

        def fake_fetch(_provider: str, **kwargs):
            cancel_check = kwargs["cancel_check"]
            callback_seen.append(callable(cancel_check))
            worker.request_stop()
            if cancel_check():
                raise CollectionCancelled("stopped")
            return []

        with (
            patch(
                "ui.workers.material_collection.fetch_exact_wear_candidates",
                side_effect=fake_fetch,
            ),
            patch("ui.workers.material_collection.close_access_sessions"),
        ):
            worker.run()

        self.assertEqual(callback_seen, [True])
        self.assertTrue(worker._is_stop_requested())

    def test_material_worker_collects_platforms_in_parallel(self) -> None:
        worker = MaterialCollectionWorker(
            materials=[{"name": "skin", "min_wear": 0.1, "max_wear": 0.2}],
            providers=["buff", "yyyp", "c5", "eco"],
            provider_intervals={key: 5 for key in ("buff", "yyyp", "c5", "eco")},
        )
        active = 0
        max_active = 0
        lock = Lock()
        page_limits: list[int] = []

        def fake_fetch(provider: str, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                page_limits.append(kwargs["max_pages"])
            time.sleep(0.05)
            with lock:
                active -= 1
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
        ):
            worker.run()

        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(page_limits, [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
