"""Qt worker 状态辅助。"""

from __future__ import annotations

from collections.abc import Iterable


def worker_is_running(worker: object) -> bool:
    if worker is None or not hasattr(worker, "isRunning"):
        return False
    try:
        return bool(worker.isRunning())
    except Exception:
        return False


def any_worker_running(workers: Iterable[object]) -> bool:
    return any(worker_is_running(worker) for worker in workers)
