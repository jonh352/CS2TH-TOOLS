"""Tests for user collection-preset storage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CollectionPresetStoreTests(unittest.TestCase):
    def test_save_list_load_delete_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("core.collection_presets.COLLECTION_PRESETS_DIR", root):
                from core.collection_presets import (
                    delete_collection_preset,
                    list_collection_presets,
                    load_collection_preset,
                    save_collection_preset,
                )

                saved = save_collection_preset(
                    title="核子",
                    items=[
                        {
                            "name": "AWP | 冥界之河",
                            "min_wear": 0.15,
                            "max_wear": 0.38,
                        },
                        {
                            "name": "P250 | 交换机",
                            "min_wear": 0.0,
                            "max_wear": 0.07,
                        },
                        # duplicate name ignored
                        {
                            "name": "AWP | 冥界之河",
                            "min_wear": 0.0,
                            "max_wear": 1.0,
                        },
                    ],
                )
                self.assertEqual(saved["title"], "核子")
                self.assertEqual(len(saved["items"]), 2)
                listed = list_collection_presets()
                self.assertEqual(len(listed), 1)
                loaded = load_collection_preset(saved["id"])
                assert loaded is not None
                self.assertEqual(loaded["items"][0]["name"], "AWP | 冥界之河")
                self.assertTrue(delete_collection_preset(saved["id"]))
                self.assertEqual(list_collection_presets(), [])


if __name__ == "__main__":
    unittest.main()
