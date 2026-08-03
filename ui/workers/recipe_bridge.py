"""Background loading for CS2TH recipe links."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.recipe_bridge import attach_recipe_alternatives, fetch_recipe_detail


class RecipeLoadThread(QThread):
    completed = Signal(object, str)

    def __init__(
        self,
        reference: str,
        access_token: str,
        parent=None,
        *,
        include_alternatives: bool = False,
    ) -> None:
        super().__init__(parent)
        self.reference = reference
        self.access_token = access_token
        self.include_alternatives = bool(include_alternatives)

    def run(self) -> None:
        try:
            payload = fetch_recipe_detail(self.reference, self.access_token)
            if self.include_alternatives:
                payload = attach_recipe_alternatives(payload, self.access_token)
            self.completed.emit(payload, "")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.completed.emit(None, str(exc))
