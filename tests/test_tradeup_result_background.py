import unittest
from types import SimpleNamespace

from ui.widgets.tradeup_original_background import result_background_color


class TradeupResultBackgroundTests(unittest.TestCase):
    def test_actual_template_grade_controls_background(self):
        for quality, color in (
            ("消费级", "#b0c3d9"), ("工业级", "#5e98d9"), ("军规级", "#4b69ff"),
            ("受限", "#8847ff"), ("保密", "#d32ce6"), ("隐秘", "#eb4b4b"), ("非凡", "#e4ae39"),
        ):
            with self.subTest(quality=quality):
                template = SimpleNamespace(weapon_name="测试武器", quality=quality)
                label, actual = result_background_color({}, template)
                self.assertEqual(label, quality)
                self.assertEqual(actual.name(), color)

    def test_steam_rarity_works_without_catalogue_template(self):
        for value in (4, "mythical", "Restricted", "受限"):
            self.assertEqual(result_background_color({"rarity": value})[1].name(), "#8847ff")
        self.assertEqual(result_background_color({"rarity_color": "d32ce6"})[1].name(), "#d32ce6")

    def test_special_knife_and_glove_do_not_use_covert_red(self):
        for name in ("★ 蝴蝶刀 | 渐变之色", "运动手套（★） | 潘多拉之盒"):
            self.assertEqual(result_background_color({"name": name, "rarity_color": "eb4b4b"})[1].name(), "#e4ae39")

    def test_stattrak_knife_with_covert_catalogue_quality_is_gold(self):
        template = SimpleNamespace(weapon_name="蝴蝶刀（★StatTrak™）", quality="隐秘")
        self.assertEqual(result_background_color({}, template)[1].name(), "#e4ae39")

    def test_unknown_grade_uses_neutral_color(self):
        self.assertEqual(result_background_color({"rarity_color": "invalid"})[1].name(), "#9ca3af")

    def test_actual_quality_overrides_stale_preview_color(self):
        template = SimpleNamespace(weapon_name="测试武器", quality="军规级")
        self.assertEqual(result_background_color({"rarity_color": "eb4b4b"}, template)[1].name(), "#4b69ff")


if __name__ == "__main__":
    unittest.main()
