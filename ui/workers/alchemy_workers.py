"""炼金页后台任务 - 获取价格、计算配方"""

from __future__ import annotations

import os
import random
import sys
from concurrent.futures import FIRST_COMPLETED, CancelledError, ProcessPoolExecutor, wait
from concurrent.futures import process as _cf_process_module
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from config import ALCHEMY_SCAN_MODE_PROCESS_POOL_MAX_WORKERS
from core.alchemy_calc import (
    ComputationCancelled,
    compute_recipes,
    prepare_scan_parallel_inputs,
    init_scan_worker_pool,
    worker_scan_single_nfv_task,
    _finalize_recipes_from_rate_results,
    load_product_price_raw,
    build_price_map,
    eligible_selected_data_for_target,
    filter_non_overlapping_recipes,
    highest_expectation_recipe,
    partition_selected_data_by_tradeup_group,
    remove_recipe_substrates_from_rows,
)
from core.process_pool_kill import shutdown_process_pool_hard
from core.alchemy_special_wear import compute_special_wear_recipes


def _recipes_to_ui_payload(recipes: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "solution"} for r in recipes]


def _tag_recipe_group(
    recipes: list[dict],
    quality: str,
    stat_trak: bool,
) -> None:
    for recipe in recipes:
        recipe["substrate_quality"] = quality
        recipe["substrate_stat_trak"] = bool(stat_trak)


def _build_non_overlapping_group_recipes(
    *,
    rows: list[dict],
    initial_recipes: list[dict],
    k: int,
    price_map: dict,
    norm_min: float,
    norm_max: float,
    mode: str,
    timeout: float,
    min_break_even_rate: float,
    max_break_even_rate: float,
    cancel_check,
) -> list[dict]:
    """Repeatedly solve, consume the winner's physical items, and solve again."""
    remaining = list(rows)
    candidates = list(initial_recipes)
    selected: list[dict] = []
    max_batches = len(remaining) // max(1, int(k))
    while len(selected) < max_batches and len(remaining) >= k:
        if cancel_check():
            raise ComputationCancelled
        winner = highest_expectation_recipe(candidates)
        if winner is None:
            break
        next_remaining = remove_recipe_substrates_from_rows(remaining, winner)
        if len(remaining) - len(next_remaining) != k:
            break
        selected.append(winner)
        remaining = next_remaining
        if len(remaining) < k:
            break
        candidates, _error = compute_recipes(
            remaining,
            price_map,
            norm_min,
            norm_max,
            mode=mode,
            timeout=timeout,
            progress_queue=None,
            cancel_check=cancel_check,
            min_break_even_rate=min_break_even_rate,
            max_break_even_rate=max_break_even_rate,
        )
    return selected


def _scan_mode_process_pool_workers(
    *, task_count: int = 1, input_count: int = 0
) -> int:
    """扫描模式 ProcessPoolExecutor 的进程数。

    非 Windows：与 ``cpu_count//2`` 一致（至少 1），并受 ``ALCHEMY_SCAN_MODE_PROCESS_POOL_MAX_WORKERS`` 上限。
    Windows：另受 CPython ``WaitForMultipleObjects`` 容量限制（``_MAX_WINDOWS_WORKERS``，当前为 61）。
    高核机器（如双路 E5）若按满核开进程，Windows spawn + 大体量 pickle 易导致整机卡顿、进度长期为 0。
    """
    cpu = max(1, os.cpu_count() or 4)
    n = max(1, min(cpu // 2, int(ALCHEMY_SCAN_MODE_PROCESS_POOL_MAX_WORKERS)))
    if task_count > 0:
        n = min(n, max(1, int(task_count)))
    # Every spawned Windows worker receives its own copy of the substrate list,
    # price map and solver caches.  Reduce fan-out for unusually large imports
    # to prevent memory pressure from killing the desktop process.  This only
    # changes parallelism; recipe inputs and the solver algorithm stay intact.
    if input_count >= 3000:
        n = min(n, 2)
    elif input_count >= 1000:
        n = min(n, 4)
    if sys.platform != "win32":
        return n
    cap = getattr(_cf_process_module, "_MAX_WINDOWS_WORKERS", 61)
    try:
        cap_i = int(cap)
    except (TypeError, ValueError):
        cap_i = 61
    return min(n, max(1, cap_i))


class FetchPriceWorker(QThread):
    """后台同步本地快照或远端价格缓存，再解析为 price_map。"""
    finished = Signal(object, object)  # (price_map, error_msg)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            if self.isInterruptionRequested():
                self.finished.emit(None, "__cancelled__")
                return
            raw = load_product_price_raw()
            if self.isInterruptionRequested():
                self.finished.emit(None, "__cancelled__")
                return
            price_map = build_price_map(raw)
            if self.isInterruptionRequested():
                self.finished.emit(None, "__cancelled__")
                return
            self.finished.emit(price_map, None)
        except Exception as e:
            self.finished.emit(None, str(e))


class _CalcPoolThread(QThread):
    """在独立线程中跑 ProcessPoolExecutor，避免阻塞 Qt 事件循环。"""
    task_finished = Signal(object, object)
    task_progress = Signal(int)

    def __init__(
        self,
        selected_data,
        price_map,
        norm_min,
        norm_max,
        mode: str,
        min_break_even_rate: float = 0.0,
        max_break_even_rate: float = 1.0,
        non_overlapping_recipes: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._selected_data = selected_data
        self._price_map = price_map
        self._norm_min = norm_min
        self._norm_max = norm_max
        self._mode = mode
        self._min_break_even_rate = max(0.0, float(min_break_even_rate))
        self._max_break_even_rate = min(1.0, max(0.0, float(max_break_even_rate)))
        self._non_overlapping_recipes = bool(non_overlapping_recipes)

    def _wait_future_result_or_cancel(
        self, fut, ex: ProcessPoolExecutor
    ) -> list | None:
        """等待单个子进程任务；期间轮询 ``isInterruptionRequested``。

        用户取消时已 ``shutdown_process_pool_hard``（含对 worker 的 kill/terminate），返回 ``None``；
        正常完成返回结果列表（子进程返回 ``None`` 时视为 ``[]``）。
        ``CancelledError`` 在池关闭后出现时返回 ``[]``。
        """
        while True:
            if self.isInterruptionRequested():
                shutdown_process_pool_hard(ex)
                return None
            try:
                r = fut.result(timeout=0.15)
                return r if r is not None else []
            except TimeoutError:
                continue
            except CancelledError:
                return []

    def run(self):
        timeout = 30.0
        try:
            groups = partition_selected_data_by_tradeup_group(
                self._selected_data,
                eligible_only=True,
            )
            if not groups:
                self.task_progress.emit(100)
                self.task_finished.emit([], None)
                return

            if self._mode == "scan":
                if self.isInterruptionRequested():
                    self.task_finished.emit([], "__cancelled__")
                    return

                prepared_groups: list[tuple[str, bool, tuple]] = []
                first_error: str | None = None
                for quality, stat_trak, _group_k, rows in groups:
                    prep = prepare_scan_parallel_inputs(
                        rows,
                        self._price_map,
                        self._norm_min,
                        self._norm_max,
                    )
                    if self.isInterruptionRequested():
                        self.task_finished.emit([], "__cancelled__")
                        return
                    if isinstance(prep, str):
                        first_error = first_error or prep
                        continue
                    prepared_groups.append((quality, stat_trak, prep))

                if not prepared_groups:
                    self.task_finished.emit([], first_error)
                    return

                total = sum(
                    len(prep[1])
                    for _quality, _stat_trak, prep in prepared_groups
                )
                completed = 0
                all_recipes: list[dict] = []
                self.task_progress.emit(0)
                prepared_group_count = len(prepared_groups)
                total_work = total + prepared_group_count
                for quality, stat_trak, prep in prepared_groups:
                    (
                        k,
                        nfv_list,
                        sorted_nfv_cache,
                        instances,
                        optional_instances,
                        required_instances,
                    ) = prep
                    n_workers = _scan_mode_process_pool_workers(
                        task_count=len(nfv_list),
                        input_count=len(instances),
                    )
                    max_inflight = max(n_workers * 2, n_workers)
                    all_raw: list[dict] = []
                    ex = ProcessPoolExecutor(
                        max_workers=n_workers,
                        initializer=init_scan_worker_pool,
                        initargs=(
                            instances,
                            optional_instances,
                            required_instances,
                            self._price_map,
                            sorted_nfv_cache,
                            k,
                            timeout,
                            self._min_break_even_rate,
                            self._max_break_even_rate,
                        ),
                    )
                    hard_cancel = False
                    try:
                        nfv_iter = iter(nfv_list)
                        pending: set = set()

                        def _submit_next() -> bool:
                            nfv = next(nfv_iter, None)
                            if nfv is None:
                                return False
                            pending.add(ex.submit(worker_scan_single_nfv_task, nfv))
                            return True

                        for _ in range(min(max_inflight, len(nfv_list))):
                            if self.isInterruptionRequested():
                                hard_cancel = True
                                shutdown_process_pool_hard(ex)
                                self.task_finished.emit([], "__cancelled__")
                                return
                            _submit_next()

                        while pending:
                            if self.isInterruptionRequested():
                                hard_cancel = True
                                shutdown_process_pool_hard(ex)
                                self.task_finished.emit([], "__cancelled__")
                                return
                            batch_done, pending = wait(
                                pending, timeout=0.12, return_when=FIRST_COMPLETED
                            )
                            for fut in batch_done:
                                chunk = self._wait_future_result_or_cancel(fut, ex)
                                if chunk is None:
                                    hard_cancel = True
                                    self.task_finished.emit([], "__cancelled__")
                                    return
                                all_raw.extend(chunk)
                                completed += 1
                                # 每个搜索任务和每个品质组的结果整理都计入总进度。
                                self.task_progress.emit(
                                    min(99, int(99 * completed / total_work))
                                )
                            while len(pending) < max_inflight:
                                if self.isInterruptionRequested():
                                    hard_cancel = True
                                    shutdown_process_pool_hard(ex)
                                    self.task_finished.emit([], "__cancelled__")
                                    return
                                if not _submit_next():
                                    break
                    finally:
                        if not hard_cancel:
                            ex.shutdown(wait=True)

                    recipes = _finalize_recipes_from_rate_results(
                        all_raw,
                        k,
                        self._price_map,
                        min_break_even_rate=self._min_break_even_rate,
                        max_break_even_rate=self._max_break_even_rate,
                    )
                    if self._non_overlapping_recipes:
                        group_rows = next(
                            rows
                            for group_quality, group_stat_trak, _group_k, rows in groups
                            if group_quality == quality
                            and group_stat_trak == stat_trak
                        )
                        recipes = _build_non_overlapping_group_recipes(
                            rows=group_rows,
                            initial_recipes=recipes,
                            k=k,
                            price_map=self._price_map,
                            norm_min=self._norm_min,
                            norm_max=self._norm_max,
                            mode="scan",
                            timeout=timeout,
                            min_break_even_rate=self._min_break_even_rate,
                            max_break_even_rate=self._max_break_even_rate,
                            cancel_check=self.isInterruptionRequested,
                        )
                    _tag_recipe_group(recipes, quality, stat_trak)
                    all_recipes.extend(recipes)
                    completed += 1
                    self.task_progress.emit(
                        min(99, int(99 * completed / total_work))
                    )
                self.task_finished.emit(_recipes_to_ui_payload(all_recipes), None)
            elif self._mode == "target":
                all_recipes: list[dict] = []
                first_error: str | None = None
                for quality, stat_trak, _k, rows in groups:
                    recipes, err = compute_recipes(
                        rows,
                        self._price_map,
                        self._norm_min,
                        self._norm_max,
                        mode="target",
                        timeout=timeout,
                        progress_queue=None,
                        cancel_check=self.isInterruptionRequested,
                        min_break_even_rate=self._min_break_even_rate,
                        max_break_even_rate=self._max_break_even_rate,
                    )
                    if err:
                        first_error = first_error or err
                        continue
                    if self._non_overlapping_recipes:
                        recipes = _build_non_overlapping_group_recipes(
                            rows=rows,
                            initial_recipes=recipes,
                            k=_k,
                            price_map=self._price_map,
                            norm_min=self._norm_min,
                            norm_max=self._norm_max,
                            mode="target",
                            timeout=timeout,
                            min_break_even_rate=self._min_break_even_rate,
                            max_break_even_rate=self._max_break_even_rate,
                            cancel_check=self.isInterruptionRequested,
                        )
                    _tag_recipe_group(recipes, quality, stat_trak)
                    all_recipes.extend(recipes)
                self.task_progress.emit(100)
                self.task_finished.emit(
                    _recipes_to_ui_payload(all_recipes),
                    None if all_recipes else first_error,
                )
            else:
                self.task_finished.emit([], f"无效的 mode: {self._mode}，仅支持 scan 或 target")
        except ComputationCancelled:
            self.task_finished.emit([], "__cancelled__")
        except Exception as e:
            self.task_finished.emit([], str(e))


class SpecialWearCalcWorker(QThread):
    """特殊磨损模式：抽样底物 + 搜索低成本组合（主线程外运行）。"""

    finished = Signal(object, object)  # (recipes, error_msg)
    progress = Signal(int)  # 0–100，与 scan 模式进度条一致
    # (checked, total_combo, nodes, n_inst, k, round_idx, total_rounds)；用 object 避免 Qt int 32 位溢出
    progress_stats = Signal(object)

    def __init__(
        self,
        selected_data: list,
        price_map: dict,
        target_paint_index: str,
        target_wear_lo: float,
        target_wear_hi: float,
        seed: Optional[int] = None,
        rounds: int = 1,
        non_overlapping_recipes: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._selected_data = selected_data
        self._price_map = price_map
        self._target_paint_index = target_paint_index
        self._target_wear_lo = target_wear_lo
        self._target_wear_hi = target_wear_hi
        self._seed = seed
        self._rounds = rounds
        self._non_overlapping_recipes = bool(non_overlapping_recipes)

    def run(self):
        try:
            calculation_data = eligible_selected_data_for_target(
                self._selected_data,
                self._target_paint_index,
            )
            groups = partition_selected_data_by_tradeup_group(
                calculation_data,
                eligible_only=True,
            )
            if not groups:
                self.progress.emit(100)
                self.finished.emit([], None)
                return
            quality, stat_trak, _k, _rows = groups[0]

            rng = random.Random(self._seed) if self._seed is not None else random.Random()
            last_pct_emitted = -1

            def on_prog(
                checked: int,
                total: int,
                nodes: int,
                pct: int,
                round_idx: int,
                total_rounds: int,
                n_inst: int,
                k_val: int,
            ) -> None:
                nonlocal last_pct_emitted
                self.progress_stats.emit(
                    (checked, total, nodes, n_inst, k_val, round_idx, total_rounds)
                )
                p = max(0, min(100, int(pct)))
                # 多轮并行时每完成一轮都会回调；重复 pct 不必刷主线程进度条，减轻 UI 卡顿
                if p != last_pct_emitted or p in (0, 100):
                    last_pct_emitted = p
                    self.progress.emit(p)

            recipes, err = compute_special_wear_recipes(
                calculation_data,
                self._price_map,
                self._target_paint_index,
                self._target_wear_lo,
                self._target_wear_hi,
                rng=rng,
                progress_callback=on_prog,
                rounds=self._rounds,
                cancel_check=self.isInterruptionRequested,
            )
            _tag_recipe_group(recipes, quality, stat_trak)
            if self._non_overlapping_recipes:
                recipes = filter_non_overlapping_recipes(recipes)
            self.finished.emit(_recipes_to_ui_payload(recipes), err)
        except ComputationCancelled:
            self.finished.emit([], "__cancelled__")
        except Exception as e:
            self.finished.emit([], str(e))


class SpecialWearCalcRunner(QObject):
    finished = Signal(object, object)
    progress = Signal(int)
    progress_stats = Signal(object)

    def __init__(
        self,
        selected_data: list,
        price_map: dict,
        target_paint_index: str,
        target_wear_lo: float,
        target_wear_hi: float,
        seed: Optional[int] = None,
        rounds: int = 1,
        non_overlapping_recipes: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._selected_data = selected_data
        self._price_map = price_map
        self._target_paint_index = target_paint_index
        self._target_wear_lo = target_wear_lo
        self._target_wear_hi = target_wear_hi
        self._seed = seed
        self._rounds = rounds
        self._non_overlapping_recipes = bool(non_overlapping_recipes)
        self._thread: SpecialWearCalcWorker | None = None

    def start(self):
        self._thread = SpecialWearCalcWorker(
            self._selected_data,
            self._price_map,
            self._target_paint_index,
            self._target_wear_lo,
            self._target_wear_hi,
            self._seed,
            self._rounds,
            self._non_overlapping_recipes,
            parent=self,
        )
        self._thread.progress.connect(self.progress.emit)
        self._thread.progress_stats.connect(self.progress_stats.emit)
        self._thread.finished.connect(self.finished.emit)
        self._thread.finished.connect(self._release_special_wear_thread_ref)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _release_special_wear_thread_ref(self) -> None:
        self._thread = None

    def cancel(self) -> None:
        t = self._thread
        if t is None:
            return
        try:
            if t.isRunning():
                t.requestInterruption()
        except RuntimeError:
            pass


class CalcProcessRunner(QObject):
    """炼金计算：扫描模式用 ProcessPoolExecutor 多进程，进度 = 已完成 future 数 / 总任务数。"""
    finished = Signal(object, object)  # (recipes, error_msg)
    progress = Signal(int)  # 0-100

    VALID_MODES = ("scan", "target")

    def __init__(
        self,
        selected_data,
        price_map,
        norm_min,
        norm_max,
        mode: str,
        min_break_even_rate: float = 0.0,
        max_break_even_rate: float = 1.0,
        non_overlapping_recipes: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        if mode not in self.VALID_MODES:
            raise ValueError(f"无效的 mode: {mode}，仅支持 {self.VALID_MODES}")
        self._selected_data = selected_data
        self._price_map = price_map
        self._norm_min = norm_min
        self._norm_max = norm_max
        self._mode = mode
        self._min_break_even_rate = max(0.0, float(min_break_even_rate))
        self._max_break_even_rate = min(1.0, max(0.0, float(max_break_even_rate)))
        self._non_overlapping_recipes = bool(non_overlapping_recipes)
        self._thread: _CalcPoolThread | None = None

    def start(self):
        self._thread = _CalcPoolThread(
            self._selected_data,
            self._price_map,
            self._norm_min,
            self._norm_max,
            self._mode,
            self._min_break_even_rate,
            self._max_break_even_rate,
            self._non_overlapping_recipes,
            parent=self,
        )
        self._thread.task_finished.connect(self.finished.emit)
        self._thread.task_progress.connect(self.progress.emit)
        self._thread.finished.connect(self._release_calc_pool_thread_ref)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _release_calc_pool_thread_ref(self) -> None:
        self._thread = None

    def cancel(self) -> None:
        t = self._thread
        if t is None:
            return
        try:
            if t.isRunning():
                t.requestInterruption()
        except RuntimeError:
            pass
