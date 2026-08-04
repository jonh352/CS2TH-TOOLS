"""Collect exact-wear listings and solve a concrete special-wear basket."""

from __future__ import annotations

from threading import Event

from PySide6.QtCore import QThread, Signal

from core.alchemy_special_wear import compute_special_wear_recipes
from core.collection_cancel import CollectionCancelled
from ui.workers.material_collection import collect_candidates_parallel


class SpecialCollectionWorker(QThread):
    progress = Signal(str)
    completed = Signal(object, object, str)

    def __init__(
        self,
        *,
        materials: list[dict],
        providers: list[str],
        provider_intervals: dict[str, int],
        target_paint_index: str,
        target_wear_low: float,
        target_wear_high: float,
        slot_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.materials = [dict(item) for item in materials]
        self.providers = list(providers)
        self.provider_intervals = dict(provider_intervals)
        self.target_paint_index = str(target_paint_index)
        self.target_wear_low = float(target_wear_low)
        self.target_wear_high = float(target_wear_high)
        self.slot_count = 5 if int(slot_count) == 5 else 10
        self._stop_event = Event()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()

    def _is_stop_requested(self) -> bool:
        return self._stop_event.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        candidates, errors, _retry_meta = collect_candidates_parallel(
            materials=self.materials,
            providers=self.providers,
            provider_intervals=self.provider_intervals,
            progress=self.progress.emit,
            cancel_check=self._is_stop_requested,
        )

        if self._is_stop_requested():
            self.completed.emit(candidates, [], "已停止采集")
            return

        if len(candidates) < self.slot_count:
            detail = "；".join(errors)
            message = (
                f"有效候选只有 {len(candidates)} 件，"
                f"至少需要 {self.slot_count} 件"
            )
            if detail:
                message += f"；{detail}"
            self.completed.emit(candidates, [], message)
            return

        self.progress.emit(
            f"候选池共 {len(candidates)} 件，"
            f"正在组合最省成本的{self.slot_count}件…"
        )
        try:
            recipes, error = compute_special_wear_recipes(
                candidates,
                {},
                self.target_paint_index,
                self.target_wear_low,
                self.target_wear_high,
                rounds=3,
                cancel_check=self._is_stop_requested,
            )
        except CollectionCancelled:
            self.completed.emit(candidates, [], "已停止采集")
            return
        except Exception as exc:  # noqa: BLE001 - keep the GUI worker alive
            if self._is_stop_requested():
                self.completed.emit(candidates, [], "已停止采集")
                return
            self.completed.emit(
                candidates,
                [],
                f"组合计算失败：{exc}",
            )
            return
        message = str(error or "")
        if errors:
            suffix = "；".join(errors)
            message = f"{message}；{suffix}".strip("；")
        self.completed.emit(candidates, recipes, message)
