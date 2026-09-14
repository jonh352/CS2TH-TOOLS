import json
import unittest
from unittest.mock import Mock, patch
from core.recipe_bridge import cs2th_detail_to_saved_recipe, saved_recipe_to_bridge_payload


class RecipeAlternativesRoundtripTests(unittest.TestCase):
    def test_alternatives_keep_original_input_indices_after_save(self):
        # Same primary at different intervals must not be regrouped: alternative
        # wear ranges belong to the original input, even after JSON string keys.
        payload = {
            "inputs": [
                {"name": "Test", "count": 2, "unit_float": 0.11, "min_wear": 0.1, "max_wear": 0.12},
                {"name": "Test", "count": 3, "unit_float": 0.12, "min_wear": 0.12, "max_wear": 0.13},
            ],
            "_alternatives_by_input": {
                0: [{"name": "Alternative A", "min_wear": 0.3, "max_wear": 0.31}],
                1: [{"name": "Alternative B", "min_wear": 0.4, "max_wear": 0.41}],
            },
            "_market": "presale",
        }
        saved = json.loads(json.dumps(cs2th_detail_to_saved_recipe(payload)))
        restored = saved_recipe_to_bridge_payload(saved)
        self.assertEqual(restored["inputs"], payload["inputs"])
        self.assertEqual(restored["_market"], "presale")
        self.assertEqual(restored["_alternatives_by_input"]["1"][0]["name"], "Alternative B")
        from ui.pages.platforms import PlatformPage
        view = Mock()
        view.include_alternatives.isChecked.return_value = True
        rows = PlatformPage._iter_recipe_display_materials(view, restored)
        self.assertEqual([item["name"] for item, is_alt in rows if is_alt],
                         ["Alternative A", "Alternative B"])
        view.include_alternatives.isChecked.return_value = False
        self.assertEqual(len(PlatformPage._iter_recipe_display_materials(view, restored)), 2)
        self.assertEqual(len(saved["substrates_display"]), 5)
        restored["inputs"][0]["name"] = "Mutated"
        self.assertEqual(saved_recipe_to_bridge_payload(saved)["inputs"][0]["name"], "Test")
        saved["substrates_display"][0]["float_value"] = 0.5
        self.assertNotIn("_alternatives_by_input", saved_recipe_to_bridge_payload(saved))

    def test_import_worker_requests_alternatives(self):
        from ui.workers.recipe_bridge import RecipeLoadThread
        worker = RecipeLoadThread("recipe", "token", include_alternatives=True)
        completed = []
        worker.completed.connect(lambda payload, error: completed.append((payload, error)))
        with patch("ui.workers.recipe_bridge.fetch_recipe_detail", return_value={"inputs": []}), patch(
            "ui.workers.recipe_bridge.attach_recipe_alternatives",
            return_value={"inputs": [], "_alternatives_by_input": {}},
        ) as attach:
            worker.run()
        attach.assert_called_once()
        self.assertEqual(completed[0][1], "")
        self.assertIn("_alternatives_by_input", completed[0][0])

