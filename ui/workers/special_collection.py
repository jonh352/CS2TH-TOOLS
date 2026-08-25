"""Collect exact-wear listings and solve a concrete special-wear basket."""

from __future__ import annotations

from threading import Event, Lock

from PySide6.QtCore import QThread, Signal

from core.alchemy_calc import ComputationCancelled
from core.alchemy_special_wear import compute_special_wear_recipes
from core.collection_cancel import CollectionCancelled
from ui.workers.material_collection import collect_candidates_parallel


class SpecialCollectionWorker(QThread):
    progress = Signal(str)
    progress_units = Signal(int, int)
    phase_changed = Signal(str)
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
        self._stop_scrape_event = Event()
        self._cancel_solve_event = Event()
        self._phase_lock = Lock()
        self._phase = "scrape"

    def phase(self) -> str:
        with self._phase_lock:
            return self._phase

    def _set_phase(self, phase: str) -> None:
        with self._phase_lock:
            self._phase = phase
        self.phase_changed.emit(phase)

    def request_stop(self) -> str:
        """Stop scraping but keep solving; a second-stage stop cancels solving."""
        phase = self.phase()
        if phase == "solve":
            self._cancel_solve_event.set()
            self.requestInterruption()
        else:
            self._stop_scrape_event.set()
        return phase

    def _is_scrape_stop_requested(self) -> bool:
        return self._stop_scrape_event.is_set()

    def _is_solve_stop_requested(self) -> bool:
        return self._cancel_solve_event.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        candidates, errors, _retry_meta = collect_candidates_parallel(
            materials=self.materials,
            providers=self.providers,
            provider_intervals=self.provider_intervals,
            progress=self.progress.emit,
            cancel_check=self._is_scrape_stop_requested,
            unit_progress=self.progress_units.emit,
        )
        scrape_stopped = self._is_scrape_stop_requested()

        if len(candidates) < self.slot_count:
            detail = "；".join(errors)
            message = (
                f"有效候选只有 {len(candidates)} 件，"
                f"至少需要 {self.slot_count} 件"
            )
            if scrape_stopped:
                message = f"已停止抓取；{message}"
            if detail:
                message += f"；{detail}"
            self.completed.emit(candidates, [], message)
            return

        self._set_phase("solve")
        if scrape_stopped:
            self.progress.emit(
                f"已停止继续抓取，正在用已有 {len(candidates)} 件候选组合"
                f"{self.slot_count}件方案…"
            )
        else:
            self.progress.emit(
                f"候选池共 {len(candidates)} 件，"
                f"正在组合最省成本的{self.slot_count}件…"
            )
        # Candidate collection and recipe solving are two distinct phases.  Reset
        # the shared progress bar here so a completed scrape is not mistaken for
        # a completed special-wear run while the 5/10-item search is still active.
        self.progress_units.emit(0, 100)

        def report_solve_progress(
            _checked: int,
            _total: int,
            _nodes: int,
            pct: int,
            _round_idx: int,
            _rounds: int,
            _n_inst: int,
            _k: int,
        ) -> None:
            progress_pct = max(0, min(100, int(pct)))
            self.progress.emit(
                f"正在从 {len(candidates)} 件候选中组合"
                f"{self.slot_count}件特殊磨损方案 · {progress_pct}%"
            )
            self.progress_units.emit(progress_pct, 100)

        try:
            recipes, error = compute_special_wear_recipes(
                candidates,
                {},
                self.target_paint_index,
                self.target_wear_low,
                self.target_wear_high,
                rounds=3,
                progress_callback=report_solve_progress,
                cancel_check=self._is_solve_stop_requested,
            )
        except (CollectionCancelled, ComputationCancelled):
            self.completed.emit(candidates, [], "已停止配组")
            return
        except Exception as exc:  # noqa: BLE001 - keep the GUI worker alive
            if self._is_solve_stop_requested():
                self.completed.emit(candidates, [], "已停止配组")
                return
            self.completed.emit(
                candidates,
                [],
                f"组合计算失败：{exc}",
            )
            return
        self.progress_units.emit(100, 100)
        message = str(error or "")
        if scrape_stopped:
            message = f"已停止抓取，已使用已有候选完成配组；{message}".strip("；")
        if errors:
            suffix = "；".join(errors)
            message = f"{message}；{suffix}".strip("；")
        self.completed.emit(candidates, recipes, message)
