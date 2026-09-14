import os
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget, QDialog
from ui.dialogs.steam_tradeup_result_dialog import SteamTradeupResultDialog, _product_source_names
from ui.dialogs.tradeup_history_product_dialog import TradeupHistoryProductDialog

class ProductRevealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent.resize(1100, 850)
        self.parent.show()
        self.dialog = SteamTradeupResultDialog(self.parent, [{"name": "Test", "float_value": 0.12}], profit=15.5)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.parent.close()

    def test_only_drag_reveals_then_continue_closes(self):
        self.assertTrue(self.dialog._continue.isHidden())
        self.dialog._advance()
        self.assertFalse(self.dialog._finished)
        self.dialog._start_reveal()
        self.assertTrue(self.dialog._reveal_cover.isHidden())
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.dialog._finished)
        self.assertEqual(self.dialog._continue.text(), "继续")
        self.assertEqual(self.dialog._animation.state(), QAbstractAnimation.State.Stopped)
        self.dialog._continue.click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_cover_contains_entire_panel_and_profit_colors(self):
        from PySide6.QtCore import QRectF
        scale = self.dialog._panel_scale()
        panel = QRectF(self.dialog.width() / 2 - 450 * scale,
                       self.dialog.height() / 2 - 240 * scale, 900 * scale, 480 * scale)
        self.assertTrue(QRectF(self.dialog._reveal_cover.geometry()).contains(panel))
        from PySide6.QtGui import QColor
        for value, positive in ((12.5, True), (-12.5, False)):
            self.dialog.profit = value
            amount, color = self.dialog._profit_display()
            self.assertIn("12.50", amount)
            tint = QColor(color)
            self.assertEqual(tint.green() > tint.red(), positive)
        self.assertEqual(self.dialog._continue.toolTip(), "")

    def test_hover_details_only_show_after_reveal_and_hide_on_leave(self):
        self.dialog.product["price"] = 88.8
        self.dialog._show_details_popup()
        self.assertTrue(self.dialog._details_popup.isHidden())
        self.dialog._start_reveal()
        self.dialog.eventFilter(self.dialog._continue, QEvent(QEvent.Type.Enter))
        self.assertFalse(self.dialog._details_popup.isHidden())
        self.assertIn("0.1200000000", self.dialog._details_popup.text())
        self.assertIn("#6bdd9a", self.dialog._details_popup.text())
        self.assertNotIn("产物参考价", self.dialog._details_popup.text())
        self.dialog.eventFilter(self.dialog._continue, QEvent(QEvent.Type.Leave))
        self.assertTrue(self.dialog._details_popup.isHidden())

    def test_completion_and_resize_preserve_result(self):
        self.dialog._start_reveal()
        self.dialog._animation.setCurrentTime(self.dialog._animation.duration())
        self.assertTrue(self.dialog._finished)
        self.dialog.resize(720, 520)
        self.app.processEvents()
        self.assertTrue(self.dialog.rect().contains(self.dialog._continue.geometry()))
        self.assertFalse(self.dialog.grab().isNull())
        self.assertEqual(self.dialog._progress, 1.0)

    def test_scaled_panel_keeps_continue_inside_with_equal_insets(self):
        from PySide6.QtCore import QRectF
        self.dialog._start_reveal()
        for width, height in ((1208, 890), (1000, 700), (720, 520)):
            self.dialog.resize(width, height)
            self.app.processEvents()
            scale = self.dialog._panel_scale()
            self.assertAlmostEqual(scale, 0.84 * min(width / 1000, height / 600))
            panel = QRectF(width / 2 - 450 * scale, height / 2 - 240 * scale,
                           900 * scale, 480 * scale)
            button = QRectF(self.dialog._continue.geometry())
            self.assertTrue(panel.contains(button))
            self.assertAlmostEqual(panel.right() - button.right(),
                                   panel.bottom() - button.bottom(), delta=1)

    def test_drag_exposes_existing_result_without_restarting_animation(self):
        cover = self.dialog._reveal_cover
        self.assertFalse(self.dialog._started)
        self.assertEqual(self.dialog._progress, 1.0)
        self.assertEqual(self.dialog._animation.state(), QAbstractAnimation.State.Stopped)
        start = cover.mapToGlobal(QPoint(40, 40))
        cover.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(40, 40), QPointF(start),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        for fraction, expected in ((0.3, False), (0.7, True)):
            end = start + QPoint(round(cover.width() * fraction), 0)
            cover.mouseMoveEvent(QMouseEvent(
                QEvent.Type.MouseMove, QPointF(40, 40), QPointF(end),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            self.assertEqual(self.dialog._started, expected)
            self.assertEqual(self.dialog._progress, 1.0)
            self.assertEqual(self.dialog._animation.state(), QAbstractAnimation.State.Stopped)
        self.assertTrue(cover.isHidden())
        self.assertTrue(self.dialog._finished)

    def test_close_stops_animation(self):
        self.dialog.reject()
        self.assertEqual(self.dialog._animation.state(), QAbstractAnimation.State.Stopped)

    def test_partial_drag_pixels_match_fully_uncovered_result(self):
        cover = self.dialog._reveal_cover
        origin = cover.pos()
        cover.move(origin + QPoint(cover.width() // 3, 0))
        self.app.processEvents()
        from PySide6.QtCore import QRect
        exposed = QRect(origin.x() + 20, origin.y() + 30,
                        cover.width() // 3 - 40, cover.height() - 60)
        partial = self.dialog.grab(exposed).toImage()
        cover.hide()
        self.app.processEvents()
        complete = self.dialog.grab(exposed).toImage()
        self.assertEqual(partial, complete)

    def test_missing_product_and_price_render(self):
        dialog = SteamTradeupResultDialog(self.parent, [], output_asset_ids=["123"])
        dialog.show()
        dialog._finish_reveal()
        self.app.processEvents()
        self.assertFalse(dialog.grab().isNull())
        dialog.close()

    def test_history_preview_remains_uncovered(self):
        dialog = TradeupHistoryProductDialog(self.parent, {"name": "Test"}, None, "")
        self.assertIsNone(dialog._reveal_cover)
        dialog.close()

    def test_history_uses_result_panel_without_continue_or_cover(self):
        dialog = SteamTradeupResultDialog(self.parent, [{"name": "Test"}],
                                         profit=-5, history_preview=True)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog._reveal_cover.isHidden())
        self.assertTrue(dialog._continue.isHidden())
        self.assertTrue(dialog._close_preview.isVisible())
        self.assertFalse(dialog.grab().isNull())
        dialog._close_preview.click()
        self.assertFalse(dialog.isVisible())

    def test_source_resolves_from_catalogue(self):
        from core.alchemy_quality import get_template_from_goods_name
        product = {"name": "AK-47 | 传承 (崭新出厂)"}
        template = get_template_from_goods_name(product["name"])
        self.assertIsNotNone(template)
        self.assertTrue(template.weapon_box_name)
        self.assertEqual(_product_source_names(product), " · ".join(template.weapon_box_name))
        dialog = SteamTradeupResultDialog(self.parent, [product])
        self.assertFalse(dialog._source_icon.isNull())
        dialog.close()

    def test_collection_badge_lookup(self):
        from core.collection_icons import collection_icon_path
        self.assertEqual(collection_icon_path("炙热沙城 II 收藏品").name, "set_dust_2_png.png")
        self.assertEqual(collection_icon_path("千瓦武器箱"), collection_icon_path("千瓦收藏品"))
        self.assertIsNone(collection_icon_path("不存在的收藏品"))

    def test_panel_background_changes_with_product_grade(self):
        colors = []
        for grade in ("eb4b4b", "8847ff"):
            dialog = SteamTradeupResultDialog(self.parent, [{"rarity_color": grade}])
            dialog.show()
            dialog._finish_reveal()
            self.app.processEvents()
            scale = dialog._panel_scale()
            sample = dialog.grab().toImage().pixelColor(
                round(dialog.width() / 2 - 300 * scale),
                round(dialog.height() / 2 - 100 * scale))
            colors.append(sample)
            dialog.close()
        self.assertGreater(colors[0].red(), colors[0].blue())
        self.assertGreater(colors[1].blue(), colors[1].red())

    def test_source_keeps_multiple_origins_and_unknown_fallback(self):
        self.assertEqual(_product_source_names({"collection": ["收藏品甲", "收藏品乙", "收藏品甲"]}),
                         "收藏品甲 · 收藏品乙")
        self.assertEqual(_product_source_names({}), "所属武器箱 / 收藏品待识别")

if __name__ == "__main__":
    unittest.main()
