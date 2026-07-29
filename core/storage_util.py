"""Local storage usage / cache cleanup for settings UI."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from config import (
    ALCHEMY_CACHE_DIR,
    AUTH_SESSION_FILE,
    BROWSER_DIR,
    CACHE_DIR,
    INVENTORY_DIR,
    MATERIAL_COLLECTION_HISTORY_FILE,
    PREFS_DIR,
    RECIPES_DIR,
    STEAM_AVATAR_CACHE_DIR,
)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += int(p.stat().st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    for unit, div in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def _rm(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
    except OSError:
        return False
    return False


def storage_usage() -> dict[str, Any]:
    total = _dir_size(CACHE_DIR)
    return {
        "ok": True,
        "bytes": total,
        "label": _fmt_bytes(total),
    }


def clear_cache() -> dict[str, Any]:
    """Clear price / avatar / collection caches; keep login and recipes."""
    removed: list[str] = []
    for path in (
        ALCHEMY_CACHE_DIR,
        STEAM_AVATAR_CACHE_DIR,
        MATERIAL_COLLECTION_HISTORY_FILE,
        CACHE_DIR / "market_browser_profiles",
    ):
        if path.exists():
            _rm(path)
            removed.append(path.name)
    ALCHEMY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STEAM_AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "removed": removed, **storage_usage()}


def clear_all_data(*, keep_settings: bool = True) -> dict[str, Any]:
    """Wipe sessions, inventory, recipes and auth; optionally keep prefs."""
    removed: list[str] = []
    targets = [
        AUTH_SESSION_FILE,
        INVENTORY_DIR,
        BROWSER_DIR,
        RECIPES_DIR,
        ALCHEMY_CACHE_DIR,
        MATERIAL_COLLECTION_HISTORY_FILE,
        CACHE_DIR / "market_auth",
        CACHE_DIR / "market_browser_profiles",
    ]
    if not keep_settings:
        targets.append(PREFS_DIR)
    for path in targets:
        if path.exists():
            _rm(path)
            removed.append(path.name)
    # Recreate expected runtime dirs
    for path in (
        PREFS_DIR,
        INVENTORY_DIR,
        BROWSER_DIR,
        ALCHEMY_CACHE_DIR,
        RECIPES_DIR,
        STEAM_AVATAR_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "removed": removed, **storage_usage()}
