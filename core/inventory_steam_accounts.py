"""库存页多 Steam 账号：索引、迁移与各账号目录下的库存与登录配置。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import (
    INVENTORY_CONFIG_FILE,
    INVENTORY_DIR,
    INVENTORY_FILE,
    STEAM_ACCOUNTS_INDEX_FILE,
    STEAM_SESSION_DIR,
)
from core.steam_session_profiles import ProfileRealm, profile_browser_dir, steam_account_storage_state_path

INDEX_VERSION = 1
MAX_INVENTORY_STEAM_ACCOUNTS = 10


def _default_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "active_profile_id": "", "profiles": []}


def load_steam_accounts_index() -> dict[str, Any]:
    try:
        if STEAM_ACCOUNTS_INDEX_FILE.exists():
            data = json.loads(STEAM_ACCOUNTS_INDEX_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and int(data.get("version", 0)) == INDEX_VERSION:
                if "profiles" not in data or not isinstance(data["profiles"], list):
                    data["profiles"] = []
                return data
    except Exception:
        pass
    return _default_index()


def save_steam_accounts_index(data: dict[str, Any]) -> None:
    STEAM_ACCOUNTS_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {**data, "version": INDEX_VERSION}
    STEAM_ACCOUNTS_INDEX_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def profile_session_root(profile_id: str) -> Path:
    return profile_browser_dir(ProfileRealm.INVENTORY, profile_id)


def profile_inventory_data_path(profile_id: str) -> Path:
    return profile_session_root(profile_id) / "inventory.json"


def profile_steam_account_config_path(profile_id: str) -> Path:
    return profile_session_root(profile_id) / "steam_account.json"


def load_steam_account_config_dict(profile_id: str) -> dict[str, str]:
    path = profile_steam_account_config_path(profile_id)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "steam_id": str(data.get("steam_id", "") or ""),
                    "steam_personaname": str(data.get("steam_personaname", "") or ""),
                    "steam_avatar_path": str(data.get("steam_avatar_path", "") or ""),
                    "steam_avatar_url": str(data.get("steam_avatar_url", "") or ""),
                }
    except Exception:
        pass
    return {
        "steam_id": "",
        "steam_personaname": "",
        "steam_avatar_path": "",
        "steam_avatar_url": "",
    }


def save_steam_account_config_dict(profile_id: str, cfg: dict[str, str]) -> None:
    path = profile_steam_account_config_path(profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "steam_id": cfg.get("steam_id", ""),
                "steam_personaname": cfg.get("steam_personaname", ""),
                "steam_avatar_path": cfg.get("steam_avatar_path", ""),
                "steam_avatar_url": cfg.get("steam_avatar_url", ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _migrate_legacy_steam_storage_to_profile(legacy_id: str) -> None:
    """将旧版写在共享目录下的 steam_storage_state 复制到首个迁移账号目录。"""
    src = steam_account_storage_state_path(STEAM_SESSION_DIR)
    dst = steam_account_storage_state_path(profile_session_root(legacy_id))
    try:
        if src.is_file() and not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except Exception:
        pass


def migrate_legacy_inventory_if_needed() -> None:
    """无索引文件时：从 inventory.json / inventory_config 迁移到单账号目录并建立索引。"""
    if STEAM_ACCOUNTS_INDEX_FILE.exists():
        return
    has_legacy = INVENTORY_CONFIG_FILE.exists() or INVENTORY_FILE.exists()
    if not has_legacy:
        save_steam_accounts_index(_default_index())
        return

    legacy_id = "legacy"
    root = profile_session_root(legacy_id)
    root.mkdir(parents=True, exist_ok=True)

    try:
        if INVENTORY_FILE.exists():
            shutil.copy2(INVENTORY_FILE, profile_inventory_data_path(legacy_id))
        else:
            profile_inventory_data_path(legacy_id).write_text("[]", encoding="utf-8")
    except Exception:
        try:
            profile_inventory_data_path(legacy_id).write_text("[]", encoding="utf-8")
        except Exception:
            pass

    cfg: dict[str, Any] = {}
    try:
        if INVENTORY_CONFIG_FILE.exists():
            raw = json.loads(INVENTORY_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = raw
    except Exception:
        pass

    steam_payload = {
        "steam_id": str(cfg.get("steam_id", "") or ""),
        "steam_personaname": str(cfg.get("steam_personaname", "") or ""),
        "steam_avatar_path": str(cfg.get("steam_avatar_path", "") or ""),
        "steam_avatar_url": str(cfg.get("steam_avatar_url", "") or ""),
    }
    save_steam_account_config_dict(legacy_id, steam_payload)
    _migrate_legacy_steam_storage_to_profile(legacy_id)

    display = (steam_payload.get("steam_personaname") or "").strip()
    if not display and (steam_payload.get("steam_id") or "").strip():
        display = f"Steam ···{steam_payload['steam_id'][-4:]}"
    if not display:
        display = "主账号"

    save_steam_accounts_index(
        {
            "version": INDEX_VERSION,
            "active_profile_id": legacy_id,
            "profiles": [{"id": legacy_id, "display_name": display}],
        }
    )


def new_profile_id() -> str:
    return uuid4().hex[:12]


def add_steam_profile(*, display_name: str = "") -> str:
    """新建空账号槽位并设为当前选中；返回 profile_id。"""
    migrate_legacy_inventory_if_needed()
    idx = load_steam_accounts_index()
    pid = new_profile_id()
    name = (display_name or "").strip() or "新账号"
    profiles = list(idx.get("profiles", []))
    profiles.append({"id": pid, "display_name": name})
    idx["profiles"] = profiles
    idx["active_profile_id"] = pid
    save_steam_accounts_index(idx)

    root = profile_session_root(pid)
    root.mkdir(parents=True, exist_ok=True)
    profile_inventory_data_path(pid).write_text("[]", encoding="utf-8")
    save_steam_account_config_dict(
        pid,
        {
            "steam_id": "",
            "steam_personaname": "",
            "steam_avatar_path": "",
            "steam_avatar_url": "",
        },
    )
    return pid


PENDING_ADD_ACCOUNT_DIRNAME = "_pending_steam_login"


def pending_add_account_root() -> Path:
    return (INVENTORY_DIR / PENDING_ADD_ACCOUNT_DIRNAME).resolve()


def prepare_pending_add_account_root() -> Path:
    """清空并创建「添加账号」临时登录目录（与单账号根目录同布局，供 login_steam_session 写入 storage_state）。"""
    migrate_legacy_inventory_if_needed()
    root = pending_add_account_root()
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    elif root.exists():
        root.unlink()
    root.mkdir(parents=True, exist_ok=True)
    return root


def discard_pending_add_account_root() -> None:
    root = pending_add_account_root()
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


def list_profile_ids_for_steam_id(steam_id: str) -> list[str]:
    """返回配置中 steam_id 与给定值（strip 后）一致的所有 profile id（索引顺序）。"""
    migrate_legacy_inventory_if_needed()
    key = (steam_id or "").strip()
    if not key:
        return []
    out: list[str] = []
    for ent in list_profile_entries():
        pid = str(ent.get("id", "") or "").strip()
        if not pid:
            continue
        cfg = load_steam_account_config_dict(pid)
        if (cfg.get("steam_id") or "").strip() == key:
            out.append(pid)
    return out


def commit_pending_steam_profile(
    pending_root: Path,
    *,
    steam_id: str,
    personaname: str,
    avatar_path: str,
    avatar_url: str,
    prefer_profile_id: str | None = None,
) -> str:
    """登录成功后在 pending_root 中应有 steam_storage_state.json；迁入正式账号。

    若该 steam_id 已有槽位：用本次登录覆盖其 storage_state 与账号配置，删掉其余同 steam_id 的重复槽位，
    列表中只保留一个；优先保留 ``prefer_profile_id``（若在重复列表中）。
    否则新建 profile。
    """
    pending_root = Path(pending_root).resolve()
    src = steam_account_storage_state_path(pending_root)
    if not src.is_file():
        raise FileNotFoundError("pending steam_storage_state.json missing")
    sid = (steam_id or "").strip()
    display = (personaname or "").strip() or "新账号"
    cfg_payload = {
        "steam_id": sid,
        "steam_personaname": personaname,
        "steam_avatar_path": avatar_path,
        "steam_avatar_url": avatar_url,
    }

    matches = list_profile_ids_for_steam_id(sid)
    if matches:
        pref = (prefer_profile_id or "").strip()
        keep = pref if pref in matches else matches[0]
        dst_root = profile_session_root(keep)
        dst_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, steam_account_storage_state_path(dst_root))
        save_steam_account_config_dict(keep, cfg_payload)
        update_profile_display_name(keep, display)
        discard_pending_add_account_root()
        for mid in matches:
            if mid != keep:
                delete_steam_profile(mid)
        set_active_profile(keep)
        return keep

    if len(list_profile_entries()) >= MAX_INVENTORY_STEAM_ACCOUNTS:
        discard_pending_add_account_root()
        raise ValueError(
            f"最多可保存{MAX_INVENTORY_STEAM_ACCOUNTS}个Steam账号，请先移除后再添加"
        )

    pid = add_steam_profile(display_name=display)
    dst_root = profile_session_root(pid)
    shutil.copy2(src, steam_account_storage_state_path(dst_root))
    save_steam_account_config_dict(pid, cfg_payload)
    discard_pending_add_account_root()
    set_active_profile(pid)
    return pid


def delete_steam_profile(profile_id: str) -> str:
    """从索引移除账号并删除其目录（含库存与登录态）。返回新的 active_profile_id，无账号时为空字符串。"""
    migrate_legacy_inventory_if_needed()
    pid = (profile_id or "").strip()
    if not pid:
        return (load_steam_accounts_index().get("active_profile_id") or "").strip()
    idx = load_steam_accounts_index()
    remain: list[dict[str, Any]] = []
    for p in idx.get("profiles", []):
        if not isinstance(p, dict):
            continue
        rid = str(p.get("id", "") or "").strip()
        if rid and rid != pid:
            remain.append(p)
    root = profile_session_root(pid)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    active = str(idx.get("active_profile_id", "") or "").strip()
    idx["profiles"] = remain
    remain_ids = {str(p.get("id", "") or "").strip() for p in remain}
    if active == pid or active not in remain_ids:
        idx["active_profile_id"] = (str(remain[0]["id"]) if remain else "")
    else:
        idx["active_profile_id"] = active
    save_steam_accounts_index(idx)
    return str(idx.get("active_profile_id") or "").strip()


def set_active_profile(profile_id: str) -> None:
    idx = load_steam_accounts_index()
    ids = {
        str(p.get("id", "")).strip()
        for p in idx.get("profiles", [])
        if isinstance(p, dict) and str(p.get("id", "")).strip()
    }
    if profile_id not in ids:
        return
    idx["active_profile_id"] = profile_id
    save_steam_accounts_index(idx)


def update_profile_display_name(profile_id: str, display_name: str) -> None:
    idx = load_steam_accounts_index()
    changed = False
    for p in idx.get("profiles", []):
        if isinstance(p, dict) and p.get("id") == profile_id:
            p["display_name"] = (display_name or "").strip()
            changed = True
            break
    if changed:
        save_steam_accounts_index(idx)


def get_active_profile_id() -> str:
    migrate_legacy_inventory_if_needed()
    return (load_steam_accounts_index().get("active_profile_id") or "").strip()


def list_profile_entries() -> list[dict[str, Any]]:
    migrate_legacy_inventory_if_needed()
    out: list[dict[str, Any]] = []
    for p in load_steam_accounts_index().get("profiles", []):
        if isinstance(p, dict) and (p.get("id") or "").strip():
            out.append(p)
    return out


def combo_display_name_for_profile(entry: dict[str, Any]) -> str:
    dn = (entry.get("display_name") or "").strip()
    if dn:
        return dn
    pid = str(entry.get("id", "") or "").strip()
    if not pid:
        return "账号"
    cfg = load_steam_account_config_dict(pid)
    pn = (cfg.get("steam_personaname") or "").strip()
    if pn:
        return pn
    sid = (cfg.get("steam_id") or "").strip()
    if len(sid) >= 4:
        return f"Steam ···{sid[-4:]}"
    return f"账号 {pid[:6]}"
