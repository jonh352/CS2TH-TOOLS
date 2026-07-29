"""User-confirmed browser login hints for marketplace shortcuts."""

from __future__ import annotations

from core.app_settings_store import load_app_settings, update_app_settings
from core.inventory_steam_accounts import (
    get_active_profile_id,
    migrate_legacy_inventory_if_needed,
    profile_session_root,
)
from core.steam_session_profiles import steam_account_storage_state_path

SETTINGS_KEY = "marketplace_login_confirmed"


def confirmed_marketplace_logins() -> dict[str, bool]:
    raw = load_app_settings().get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(key): bool(value) for key, value in raw.items()}


def set_marketplace_login_confirmed(key: str, confirmed: bool) -> None:
    values = confirmed_marketplace_logins()
    values[str(key)] = bool(confirmed)
    update_app_settings(updates={SETTINGS_KEY: values})


def clear_confirmed_marketplace_logins() -> None:
    update_app_settings(updates={SETTINGS_KEY: {}})


def steam_session_available() -> bool:
    try:
        migrate_legacy_inventory_if_needed()
        profile_id = get_active_profile_id()
        return bool(profile_id) and steam_account_storage_state_path(
            profile_session_root(profile_id)
        ).is_file()
    except (OSError, ValueError):
        return False
