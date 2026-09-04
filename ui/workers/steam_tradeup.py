"""Background worker for local Steam/CS2 one-click trade-ups."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from core.purchase_batches import (
    record_inventory_recipe_tradeup_result,
    record_purchase_batch_recipe_tradeup_result,
    resolve_steam_tradeup_products,
)
from core.steam_tradeup import (
    SteamTradeupBridge,
    SteamTradeupCancelledError,
    SteamTradeupError,
)


class SteamTradeupWorker(QThread):
    status = Signal(str)
    qr_ready = Signal(str)
    irreversible = Signal()
    completed = Signal(bool, str, object)

    def __init__(
        self,
        plan: dict[str, Any],
        parent=None,
        *,
        auth: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = dict(plan)
        self._auth = dict(auth or {})
        self._bridge = SteamTradeupBridge()

    def cancel(self) -> bool:
        return self._bridge.cancel()

    def _on_event(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event") or "")
        if event_name == "status":
            self.status.emit(str(event.get("message") or "正在处理…"))
        elif event_name == "qr":
            self.qr_ready.emit(str(event.get("url") or ""))
        elif event_name == "crafting":
            self.irreversible.emit()
            self.status.emit(str(event.get("message") or "正在执行汰换…"))

    def run(self) -> None:
        try:
            result = self._bridge.run(
                self._plan,
                self._on_event,
                auth=self._auth,
            )
            output_items = [
                dict(value) for value in result.get("outputItems") or []
                if isinstance(value, dict)
            ]
            result = {
                **result,
                "resolvedProducts": resolve_steam_tradeup_products(output_items),
            }
            if self._plan.get("batch_path") and self._plan.get("recipe_entry_id"):
                try:
                    record_purchase_batch_recipe_tradeup_result(
                        Path(str(self._plan["batch_path"])),
                        str(self._plan["recipe_entry_id"]),
                        input_asset_ids=[str(value) for value in self._plan["asset_ids"]],
                        output_asset_ids=[
                            str(value) for value in result.get("outputAssetIds") or []
                        ],
                        output_items=output_items,
                        gc_recipe=int(result.get("recipe") or 0),
                    )
                except Exception as exc:
                    self.completed.emit(
                        False,
                        f"Steam 汰换已成功，但本地记录失败：{exc}。请刷新库存后手动标记为已汰换",
                        {**result, "craft_succeeded": True},
                    )
                    return
                message = "汰换成功，采购配方已标记为已汰换"
            else:
                try:
                    record_inventory_recipe_tradeup_result(
                        self._plan,
                        output_asset_ids=[
                            str(value) for value in result.get("outputAssetIds") or []
                        ],
                        output_items=output_items,
                        gc_recipe=int(result.get("recipe") or 0),
                    )
                except Exception as exc:
                    self.completed.emit(
                        False,
                        f"Steam 汰换已成功，但写入炼金记录失败：{exc}。请到采购管理「炼金记录」核对",
                        {**result, "craft_succeeded": True},
                    )
                    return
                message = "汰换成功，已写入炼金记录"
            self.completed.emit(True, message, result)
        except SteamTradeupCancelledError:
            self.completed.emit(False, "已取消一键汰换", {"cancelled": True})
        except SteamTradeupError as exc:
            self.completed.emit(
                False,
                str(exc),
                {
                    "uncertain": exc.code == "CRAFT_TIMEOUT",
                    "error_code": exc.code,
                },
            )
        except Exception as exc:
            self.completed.emit(False, str(exc), {})
