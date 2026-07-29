"""Background loading for CS2TH recipe links."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.recipe_bridge import fetch_recipe_detail


class RecipeLoadThread(QThread):
    completed = Signal(object, str)

    def __init__(self, reference: str, access_token: str, parent=None) -> None:
        super().__init__(parent)
        self.reference = reference
        self.access_token = access_token

    def run(self) -> None:
        try:
            self.completed.emit(
                fetch_recipe_detail(self.reference, self.access_token),
                "",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.completed.emit(None, str(exc))
