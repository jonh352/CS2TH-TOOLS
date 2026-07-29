"""Instantiate and cycle all pages using Qt's offscreen platform."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


app = QApplication.instance() or QApplication([])
window = MainWindow()
window.show()
app.processEvents()
for key in (
    "alchemy",
    "simulation",
    "recipes",
    "special",
    "inventory",
    "platforms",
):
    window._activate(key)
    app.processEvents()

inventory = window.pages["inventory"]
sample_inventory_item = {
    "assetid": "smoke-42",
    "market_hash_name": "AK-47 | Inheritance (Factory New)",
    "market_name": "AK-47 | Inheritance (Factory New)",
    "float": 0.03,
    "rarity": "ancient_weapon",
}
alchemy_row = inventory._to_alchemy_item(sample_inventory_item)
assert alchemy_row is not None
window.pages["alchemy"].apply_inventory_import_replace([alchemy_row])
assert len(window.pages["alchemy"]._all_data) == 1
error = window.pages["simulation"].import_inventory_items(
    [sample_inventory_item], slot_count=5
)
assert error is None
assert window.pages["simulation"].filled_substrate_count_for_slot_count(5) == 1

window.close()
print("Qt smoke test OK")
