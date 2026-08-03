from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.market_candidates import fetch_youpin_candidates


class _Response:
    status_code = 200

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"Code": 0, "Data": {"CommodityList": self._items}}


class MarketCandidatePaginationTests(unittest.TestCase):
    def test_youpin_bucket_fetches_past_two_pages_without_subdivision(self) -> None:
        calls: list[dict] = []

        def fake_post(_url, *, headers, json, timeout):
            del headers, timeout
            calls.append(dict(json))
            page = int(json["pageIndex"])
            count = 40 if page in {1, 2} else 1
            start = (page - 1) * 40
            return _Response(
                [
                    {
                        "Abrade": 0.22 + (start + index) / 10_000_000,
                        "Price": 10 + index,
                        "Id": f"listing-{start + index}",
                    }
                    for index in range(count)
                ]
            )

        template = SimpleNamespace(yyyp={"略有磨损": 42})
        with (
            patch("core.market_candidates._youpin_auth", return_value=("token", "cookie")),
            patch("core.market_candidates._merge_platform_ids", return_value=[42]),
            patch("core.market_candidates.requests.post", side_effect=fake_post),
            patch("core.market_candidates.interruptible_wait"),
        ):
            rows = fetch_youpin_candidates(
                template=template,
                display_name="测试饰品",
                min_wear=0.21,
                max_wear=0.24,
                max_pages=0,
                request_interval=5,
            )

        self.assertEqual(len(rows), 81)
        self.assertEqual([call["pageIndex"] for call in calls], [1, 2, 3])
        self.assertTrue(all(call["minAbrade"] == 0.21 for call in calls))
        self.assertTrue(all(call["maxAbrade"] == 0.24 for call in calls))


if __name__ == "__main__":
    unittest.main()
