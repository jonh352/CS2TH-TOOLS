"""Background Steam inventory-history synchronization."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.purchase_batches import apply_steam_tradeup_history
from core.steam_inventory_history import (
    SteamInventoryHistoryError,
    fetch_steam_tradeup_history,
)


class SteamHistorySyncWorker(QThread):
    status = Signal(str)
    completed = Signal(bool, str, int)

    def __init__(self, profile_ids: list[str], parent=None) -> None:
        super().__init__(parent)
        self._profile_ids = [str(value) for value in profile_ids if str(value)]

    def run(self) -> None:
        try:
            event_count = 0
            updated = 0
            errors: list[str] = []
            successful_profiles = 0
            for profile_id in self._profile_ids:
                try:
                    events = fetch_steam_tradeup_history(
                        profile_id,
                        on_status=self.status.emit,
                    )
                    successful_profiles += 1
                    event_count += len(events)
                    updated += apply_steam_tradeup_history(profile_id, events)
                except SteamInventoryHistoryError as exc:
                    errors.append(str(exc))
            if errors and not successful_profiles:
                self.completed.emit(False, errors[0], 0)
                return
            suffix = f"；另有 {len(errors)} 个账号登录失效" if errors else ""
            self.completed.emit(
                True,
                f"已读取 {event_count} 条 Steam 炼金事件，补全 {updated} 条本地记录{suffix}",
                updated,
            )
        except SteamInventoryHistoryError as exc:
            self.completed.emit(False, str(exc), 0)
        except Exception as exc:
            self.completed.emit(False, f"炼金记录刷新失败：{exc}", 0)
