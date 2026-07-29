"""应用通用偏好（与 theme_prefs 分离）。"""

from __future__ import annotations

from core.app_settings_store import load_app_settings, update_app_settings

CLOSE_MAIN_WINDOW_TRAY = "tray"
CLOSE_MAIN_WINDOW_QUIT = "quit"

_ALCHEMY_WEAR_STEP2_NOTICE_DISMISSED_KEY = "alchemy_wear_step2_notice_dismissed"
_LAST_RECIPE_SAVE_FOLDER_KEY = "last_recipe_save_folder_id"
_ALCHEMY_RECIPE_SAVE_MODE_KEY = "alchemy_recipe_save_mode"
_ALCHEMY_RECIPE_SAVE_MODE_DEFAULT = "default"
_ALCHEMY_RECIPE_SAVE_MODE_CUSTOM = "custom"
_MAIN_WINDOW_ALWAYS_ON_TOP_KEY = "main_window_always_on_top"
_ALCHEMY_STEP2_WEAR_UI_KEY = "alchemy_step2_wear_ui"


def _load_setting(key: str) -> object:
    return load_app_settings().get(key)


def _save_setting(key: str, value: object) -> None:
    update_app_settings(updates={key: value})


def load_close_main_window_action() -> str:
    v = str(_load_setting("close_main_window") or CLOSE_MAIN_WINDOW_QUIT).strip()
    if v in (CLOSE_MAIN_WINDOW_TRAY, CLOSE_MAIN_WINDOW_QUIT):
        return v
    return CLOSE_MAIN_WINDOW_QUIT


def save_close_main_window_action(action: str) -> None:
    if action not in (CLOSE_MAIN_WINDOW_TRAY, CLOSE_MAIN_WINDOW_QUIT):
        return
    _save_setting("close_main_window", action)


def load_last_recipe_save_folder_id() -> str | None:
    """无记录时返回 None；JSON null 与 \"\" 表示未分类；否则为文件夹 id。"""
    data = load_app_settings()
    if _LAST_RECIPE_SAVE_FOLDER_KEY in data:
        v = data.get(_LAST_RECIPE_SAVE_FOLDER_KEY)
        if v is None:
            return ""
        if isinstance(v, str):
            return v
    return None


def save_last_recipe_save_folder_id(folder_id: str | None) -> None:
    """None 与 \"\" 均记为未分类（JSON null）。"""
    if not folder_id or not str(folder_id).strip():
        _save_setting(_LAST_RECIPE_SAVE_FOLDER_KEY, None)
        return
    _save_setting(_LAST_RECIPE_SAVE_FOLDER_KEY, str(folder_id).strip())


def load_alchemy_wear_step2_notice_dismissed() -> bool:
    """炼金第二步磨损说明弹窗：用户勾选「不再显示」后为 True。"""
    return bool(_load_setting(_ALCHEMY_WEAR_STEP2_NOTICE_DISMISSED_KEY))


def load_alchemy_recipe_save_mode() -> str:
    """炼金第三步「配方保存方式」：默认 / 自定义；无记录或非法值时视为 default。"""
    v = str(_load_setting(_ALCHEMY_RECIPE_SAVE_MODE_KEY) or "").strip()
    if v == _ALCHEMY_RECIPE_SAVE_MODE_CUSTOM:
        return _ALCHEMY_RECIPE_SAVE_MODE_CUSTOM
    return _ALCHEMY_RECIPE_SAVE_MODE_DEFAULT


def save_alchemy_recipe_save_mode(mode: str) -> None:
    if mode not in (_ALCHEMY_RECIPE_SAVE_MODE_DEFAULT, _ALCHEMY_RECIPE_SAVE_MODE_CUSTOM):
        return
    _save_setting(_ALCHEMY_RECIPE_SAVE_MODE_KEY, mode)


def load_main_window_always_on_top() -> bool:
    """状态栏置顶图钉：上次退出是否为置顶。"""
    return _load_setting(_MAIN_WINDOW_ALWAYS_ON_TOP_KEY) is True


def save_main_window_always_on_top(enabled: bool) -> None:
    _save_setting(_MAIN_WINDOW_ALWAYS_ON_TOP_KEY, bool(enabled))


def load_alchemy_step2_wear_ui() -> dict | None:
    """炼金计算设置：磨损模式、特殊磨损轮数与配方材料去重开关。"""
    raw = _load_setting(_ALCHEMY_STEP2_WEAR_UI_KEY)
    return raw if isinstance(raw, dict) else None


def save_alchemy_step2_wear_ui(payload: dict) -> None:
    """写入 ``alchemy_step2_wear_ui``；与现有 app_settings 合并。"""
    if not isinstance(payload, dict):
        return
    _save_setting(_ALCHEMY_STEP2_WEAR_UI_KEY, payload)


def save_alchemy_wear_step2_notice_dismissed(dismissed: bool) -> None:
    if not dismissed:
        return
    _save_setting(_ALCHEMY_WEAR_STEP2_NOTICE_DISMISSED_KEY, True)
