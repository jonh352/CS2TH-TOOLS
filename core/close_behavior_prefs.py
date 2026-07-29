"""Close-window behavior preference (minimize vs exit)."""

from __future__ import annotations

from config import (
    APP_SETTINGS_CLOSE_BEHAVIOR_KEY,
    CLOSE_BEHAVIOR_EXIT,
    CLOSE_BEHAVIOR_MINIMIZE,
)

from .app_settings_store import load_app_settings, update_app_settings


def load_close_behavior() -> str:
    value = str(load_app_settings().get(APP_SETTINGS_CLOSE_BEHAVIOR_KEY) or "").strip().lower()
    if value == CLOSE_BEHAVIOR_MINIMIZE:
        return CLOSE_BEHAVIOR_MINIMIZE
    return CLOSE_BEHAVIOR_EXIT


def save_close_behavior(behavior: str) -> None:
    if behavior not in (CLOSE_BEHAVIOR_MINIMIZE, CLOSE_BEHAVIOR_EXIT):
        return
    update_app_settings(updates={APP_SETTINGS_CLOSE_BEHAVIOR_KEY: behavior})
