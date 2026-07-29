"""app_settings.json 的基础读写。"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from config import APP_SETTINGS_FILE

from .json_store import JsonDict, read_json_dict, update_json_dict


def load_app_settings() -> JsonDict:
    """读取应用通用偏好；始终返回 dict。"""
    return read_json_dict(APP_SETTINGS_FILE)


def update_app_settings(
    *,
    updates: Mapping[str, Any] | None = None,
    mutator: Callable[[JsonDict], None] | None = None,
) -> bool:
    """合并更新 app_settings.json。"""
    return update_json_dict(
        APP_SETTINGS_FILE,
        updates=updates,
        mutator=mutator,
        ensure_parent=True,
    )
