"""``ProcessPoolExecutor`` 取消后对仍存活的 worker 子进程尽最大努力强杀。"""

from __future__ import annotations

import logging
import multiprocessing.queues as mp_queues
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger(__name__)
_MP_FEEDER_PATCHED = False


def _snapshot_pool_worker_processes(executor: ProcessPoolExecutor) -> list:
    """在 ``shutdown`` 之前抓取 worker 列表。

    CPython 3.12+ 在 ``shutdown(wait=False)`` 返回后常将 ``_processes`` 置为 ``None``，
    若先 shutdown 再遍历 executor，则无法 ``kill``，子进程会继续跑满 MITM 任务。
    """
    raw = getattr(executor, "_processes", None)
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [p for p in raw.values() if p is not None]
    try:
        return [p for p in raw if p is not None]
    except TypeError:
        return []


def _terminate_worker_process_list(procs: list) -> None:
    for p in procs:
        if p is None:
            continue
        try:
            if not p.is_alive():
                continue
        except (AttributeError, ValueError, OSError):
            continue
        try:
            kfn = getattr(p, "kill", None)
            if callable(kfn):
                kfn()
            else:
                p.terminate()
        except Exception as e:
            logger.debug("process pool worker kill failed: %s", e)


def _silence_queue_feeder_closed_handle_error(executor: ProcessPoolExecutor) -> None:
    """取消时静默 call_queue feeder 的 ``OSError: handle is closed`` 噪声回溯。"""
    call_queue = getattr(executor, "_call_queue", None)
    if call_queue is None:
        return
    try:
        setattr(call_queue, "_ignore_epipe", True)
    except Exception:
        pass
    original_onerror = getattr(call_queue, "_on_queue_feeder_error", None)
    if not callable(original_onerror):
        return

    def _quiet_onerror(exc: BaseException, obj) -> None:
        if isinstance(exc, OSError) and str(exc) == "handle is closed":
            logger.debug("suppressed multiprocessing queue feeder noise: %s", exc)
            return
        original_onerror(exc, obj)

    try:
        setattr(call_queue, "_on_queue_feeder_error", _quiet_onerror)
    except Exception:
        pass


def _install_global_mp_queue_feeder_filter() -> None:
    """全局静默 multiprocessing feeder 的 ``OSError: handle is closed`` 噪声回溯。"""
    global _MP_FEEDER_PATCHED
    if _MP_FEEDER_PATCHED:
        return
    original = getattr(mp_queues.Queue, "_on_queue_feeder_error", None)
    if not callable(original):
        return

    @staticmethod
    def _quiet_on_queue_feeder_error(exc: BaseException, obj) -> None:
        if isinstance(exc, OSError) and str(exc) == "handle is closed":
            logger.debug("suppressed global multiprocessing feeder noise: %s", exc)
            return
        original(exc, obj)

    try:
        setattr(mp_queues.Queue, "_on_queue_feeder_error", _quiet_on_queue_feeder_error)
        _MP_FEEDER_PATCHED = True
    except Exception:
        pass


_install_global_mp_queue_feeder_filter()


def kill_process_pool_workers(executor: ProcessPoolExecutor) -> None:
    """遍历 CPython 内部持有的 ``multiprocessing.Process``，对仍存活者调用 ``kill`` 或 ``terminate``。"""
    _terminate_worker_process_list(_snapshot_pool_worker_processes(executor))


def shutdown_process_pool_hard(executor: ProcessPoolExecutor) -> None:
    """``shutdown(wait=False, cancel_futures=True)`` 后对 worker 强杀，避免子进程仍长时间占用 CPU。"""
    _silence_queue_feeder_closed_handle_error(executor)
    procs = _snapshot_pool_worker_processes(executor)
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    _terminate_worker_process_list(procs)
