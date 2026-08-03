"""Cooperative cancellation helpers for long-running market collection."""

from __future__ import annotations

import time
from typing import Callable

CancelCheck = Callable[[], bool] | None


class CollectionCancelled(RuntimeError):
    """Raised when the user requests cancellation of a collection run."""


def raise_if_cancelled(cancel_check: CancelCheck = None) -> None:
    if cancel_check is not None and cancel_check():
        raise CollectionCancelled("采集已由用户停止")


def interruptible_wait(seconds: float, cancel_check: CancelCheck = None) -> None:
    """Sleep in short slices so the stop button reacts promptly."""

    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        raise_if_cancelled(cancel_check)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))
