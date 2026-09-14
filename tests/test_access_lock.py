from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.access_lock import apply_page_interaction_lock, set_page_control_enabled
from ui.pages.alchemy import AlchemyPage
from ui.pages.inventory import InventoryPage


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

    def test_control_state_loaded_while_locked_is_restored_on_unlock(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        button = QPushButton("Fetch", root)
        button.setEnabled(False)
        layout.addWidget(button)

        apply_page_interaction_lock(root, True)
        set_page_control_enabled(root, button, True)
        self.assertFalse(button.isEnabled())

        apply_page_interaction_lock(root, False)
        self.assertTrue(button.isEnabled())

    def test_inventory_fetch_is_enabled_after_restart_access_unlock(self) -> None:
        profiles = [{"id": "ten-profile", "display_name": "Ten"}]
        with (
            TemporaryDirectory() as temp_dir,
            patch("ui.pages.inventory.list_profile_entries", return_value=profiles),
            patch("ui.pages.inventory.get_active_profile_id", return_value="ten-profile"),
            patch(
                "ui.pages.inventory.profile_inventory_data_path",
                return_value=Path(temp_dir) / "inventory.json",
            ),
        ):
            page = InventoryPage()
            apply_page_interaction_lock(page, True)
            self.app.processEvents()
            self.assertFalse(page.fetch_button.isEnabled())

            apply_page_interaction_lock(page, False)

        self.assertEqual(page.account_combo.currentData(), "ten-profile")
        self.assertTrue(page.fetch_button.isEnabled())

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
