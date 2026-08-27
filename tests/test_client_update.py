from __future__ import annotations

import unittest

from core.client_update import (
    ClientUpdateInfo,
    is_version_older,
    parse_client_update,
)


class ClientUpdateTests(unittest.TestCase):
    def test_parse_client_update_from_nested_payload(self) -> None:
        info = parse_client_update(
            {
                "client_update": {
                    "latest_version": "1.3.9",
                    "download_url": "https://cs2th.cn/tradeup-assistant",
                }
            }
        )
        self.assertEqual(
            info,
            ClientUpdateInfo("1.3.9", "https://cs2th.cn/tradeup-assistant"),
        )

    def test_parse_client_update_from_flat_payload(self) -> None:
        info = parse_client_update(
            {
                "latest_version": "1.3.9",
                "download_url": "https://cs2th.cn/tradeup-assistant",
            }
        )
        self.assertEqual(
            info,
            ClientUpdateInfo("1.3.9", "https://cs2th.cn/tradeup-assistant"),
        )

    def test_is_version_older_compares_semver_like_values(self) -> None:
        self.assertTrue(is_version_older("1.3.7", "1.3.9"))
        self.assertFalse(is_version_older("1.3.9", "1.3.9"))
        self.assertFalse(is_version_older("1.4.0", "1.3.9"))


if __name__ == "__main__":
    unittest.main()
