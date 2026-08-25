from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox, QVBoxLayout, QWidget

from ui.access_lock import apply_page_interaction_lock
from ui.pages.alchemy import AlchemyPage


class AccessLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_unlock_restores_spinbox_internal_editor(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        spin = QSpinBox(root)
        spin.setRange(0, 100)
        spin.setSuffix("%")
        line = QLineEdit(root)
        layout.addWidget(spin)
        layout.addWidget(line)

        apply_page_interaction_lock(root, True)
        self.assertFalse(spin.isEnabled())
        self.assertFalse(line.isEnabled())

        apply_page_interaction_lock(root, False)
        self.assertTrue(spin.isEnabled())
        self.assertTrue(spin.lineEdit().isEnabled())
        self.assertTrue(line.isEnabled())

        spin.setFocus()
        spin.selectAll()
        QTest.keyClicks(spin, "35")
        self.assertEqual(spin.value(), 35)

    def test_alchemy_break_even_range_defaults_and_accepts_integers(self) -> None:
        page = AlchemyPage()
        self.assertEqual(page.step3_min_be_spin.value(), 0)
        self.assertEqual(page.step3_max_be_spin.value(), 100)

        page.step3_max_be_spin.setFocus()
        page.step3_max_be_spin.selectAll()
        QTest.keyClicks(page.step3_max_be_spin, "65")
        self.assertEqual(page.step3_max_be_spin.value(), 65)


if __name__ == "__main__":
    unittest.main()
