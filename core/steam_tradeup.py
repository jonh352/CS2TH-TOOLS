"""Local Steam/CS2 Game Coordinator bridge for one-click trade-ups."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from core.auth_client import _protect_token, _unprotect_token
from core.inventory_steam_accounts import (
    load_steam_account_config_dict,
    profile_session_root,
)
from core.purchase_tracking import (
    inventory_item_template_key,
    inventory_wear_matches_planned,
    load_profile_inventory_items,
    recipe_substrate_template_key,
)
from core.alchemy_quality import get_template_from_goods_name
from core.data_utils import tradeup_display_quality
from core.purchase_batches import inventory_item_tradeup_cd_readiness


class SteamTradeupError(RuntimeError):
    """Raised when Steam login, GC validation, or crafting fails."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class SteamTradeupCancelledError(SteamTradeupError):
    """Raised when the user cancels before the craft request is sent."""


def inventory_recipe_tradeup_readiness(
    recipe: dict[str, Any],
    *,
    verify_inventory: bool = False,
) -> tuple[bool, str]:
    """Validate that a recipe owns 10 items, or 5 Covert items, from one account."""
    substrates = [
        row
        for row in recipe.get("substrates_display") or []
        if isinstance(row, dict)
    ]
    material_count = len(substrates)
    if material_count not in (5, 10):
        return False, "一键汰换仅支持 10 件材料或 5 件隐秘级材料"
    if material_count == 5:
        qualities = []
        for row in substrates:
            template = get_template_from_goods_name(str(row.get("name") or ""))
            qualities.append(tradeup_display_quality(template) if template else "")
        if any(quality != "隐秘" for quality in qualities):
            return False, "五合一仅支持 5 件隐秘级材料"
    asset_ids = [str(row.get("steam_assetid") or "").strip() for row in substrates]
    profile_ids = {
        str(row.get("steam_profile_id") or "").strip() for row in substrates
    }
    steam_ids = {str(row.get("steam_id") or "").strip() for row in substrates}
    if any(not asset_id for asset_id in asset_ids):
        return False, "配方没有保留全部 Steam 资产编号"
    if len(set(asset_ids)) != material_count:
        return False, "配方中存在重复的 Steam 资产编号"
    if len(profile_ids) != 1 or "" in profile_ids:
        return False, "配方材料不属于同一个本地 Steam 账号"
    if len(steam_ids) != 1 or "" in steam_ids:
        return False, "配方材料缺少一致的 Steam 账号信息"
    if not verify_inventory:
        return True, "真实库存材料，可先模拟再一键汰换"

    profile_id = next(iter(profile_ids))
    steam_id = next(iter(steam_ids))
    configured_steam_id = str(
        load_steam_account_config_dict(profile_id).get("steam_id") or ""
    )
    if not configured_steam_id or configured_steam_id != steam_id:
        return False, "当前本地 Steam 账号与配方账号不一致"
    inventory_by_id = {
        str(item.get("assetid") or ""): item
        for item in load_profile_inventory_items(profile_id)
        if str(item.get("assetid") or "")
    }
    for substrate, asset_id in zip(substrates, asset_ids):
        item = inventory_by_id.get(asset_id)
        if item is None:
            return False, "部分指定材料已不在当前本地库存，请先刷新 Steam 库存"
        if inventory_item_template_key(item) != recipe_substrate_template_key(substrate):
            return False, "库存材料名称与模拟配方不一致，请重新导入"
        if not inventory_wear_matches_planned(
            substrate.get("float_value"),
            item.get("float", item.get("float_value")),
        ):
            return False, "库存材料磨损与模拟配方不一致，请重新导入"
    return True, f"已核对 {material_count} 件真实库存材料"


def tradeup_plan_cached_readiness(
    plan: dict[str, Any],
) -> tuple[bool, str]:
    """Final desktop preflight before opening the irreversible GC dialog."""
    profile_id = str(plan.get("profile_id") or "").strip()
    expected_steam_id = str(plan.get("steam_id") or "").strip()
    asset_ids = [str(value or "").strip() for value in plan.get("asset_ids") or []]
    if not profile_id or not expected_steam_id:
        return False, "配方没有绑定有效的 Steam 账号"
    if len(asset_ids) not in (5, 10) or any(not value for value in asset_ids):
        return False, "配方材料的 Steam 资产编号无效"
    if len(set(asset_ids)) != len(asset_ids):
        return False, "配方中存在重复的 Steam 资产编号"
    configured_steam_id = str(
        load_steam_account_config_dict(profile_id).get("steam_id") or ""
    )
    if configured_steam_id != expected_steam_id:
        return False, "当前本地 Steam 账号与配方账号不一致"
    inventory_by_id = {
        str(item.get("assetid") or ""): item
        for item in load_profile_inventory_items(profile_id)
        if isinstance(item, dict) and str(item.get("assetid") or "")
    }
    missing = [asset_id for asset_id in asset_ids if asset_id not in inventory_by_id]
    if missing:
        return False, f"有 {len(missing)} 件材料不在当前库存，请刷新 Steam 库存后重试"
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for index, asset_id in enumerate(asset_ids, start=1):
        ready, reason = inventory_item_tradeup_cd_readiness(
            inventory_by_id[asset_id], now=now
        )
        if not ready:
            return False, f"第 {index} 件{reason}"
    return True, f"已确认 {len(asset_ids)} 件材料均在库存且 CD 已结束"


def build_inventory_recipe_tradeup_plan(
    recipe: dict[str, Any],
    *,
    title: str = "Steam 库存配方",
) -> dict[str, Any]:
    ready, reason = inventory_recipe_tradeup_readiness(
        recipe,
        verify_inventory=True,
    )
    if not ready:
        raise SteamTradeupError(reason)
    substrates = list(recipe.get("substrates_display") or [])
    profile_id = str(substrates[0].get("steam_profile_id") or "")
    steam_id = str(substrates[0].get("steam_id") or "")
    materials = [
        {
            "asset_id": str(row.get("steam_assetid") or ""),
            "name": str(row.get("name") or "未知材料"),
            "float_value": float(row.get("float_value") or 0),
        }
        for row in substrates
    ]
    return {
        "source": "steam_inventory_recipe",
        "title": str(title or "Steam 库存配方"),
        "profile_id": profile_id,
        "steam_id": steam_id,
        "asset_ids": [row["asset_id"] for row in materials],
        "materials": materials,
    }


def _node_executable() -> Path:
    system_node = shutil.which("node")
    if system_node:
        return Path(system_node)
    try:
        from playwright._impl._driver import compute_driver_executable

        node, _driver = compute_driver_executable()
        if Path(node).is_file():
            return Path(node)
    except Exception:
        pass
    raise SteamTradeupError("未找到本地 Steam 授权组件，请重新安装最新版客户端")


def _bridge_script() -> Path:
    path = PROJECT_ROOT / "steam_gc_bridge" / "bridge.js"
    if not path.is_file():
        raise SteamTradeupError("一键汰换组件缺失，请重新安装最新版客户端")
    return path


def _session_file(profile_id: str) -> Path:
    return profile_session_root(profile_id) / "steam_gc_session.json"


def _load_refresh_token(profile_id: str) -> str:
    try:
        payload = json.loads(_session_file(profile_id).read_text(encoding="utf-8"))
        return _unprotect_token(str(payload.get("protected_refresh_token") or ""))
    except Exception:
        return ""


def has_saved_tradeup_session(profile_id: str) -> bool:
    """Return whether a reusable local Steam Client authorization is available."""
    return bool(_load_refresh_token(str(profile_id or "").strip()))


def _save_refresh_token(profile_id: str, token: str, steam_id: str = "") -> None:
    target = _session_file(profile_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "protected_refresh_token": _protect_token(token),
                "steam_id": str(steam_id or ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def clear_tradeup_session(profile_id: str) -> None:
    try:
        _session_file(profile_id).unlink(missing_ok=True)
    except OSError:
        pass


class SteamTradeupBridge:
    """Run the Node Steam/GC helper and expose only sanitized JSON events."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._irreversible = False

    @property
    def irreversible(self) -> bool:
        return self._irreversible

    def cancel(self) -> bool:
        """Cancel only while login/validation is still reversible."""
        with self._lock:
            if self._irreversible:
                return False
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        return True

    def run(
        self,
        plan: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        auth: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        profile_id = str(plan.get("profile_id") or "").strip()
        expected_steam_id = str(plan.get("steam_id") or "").strip()
        asset_ids = [str(value) for value in plan.get("asset_ids") or []]
        if not profile_id or not expected_steam_id or len(asset_ids) not in (5, 10):
            raise SteamTradeupError("一键汰换参数无效，请重新核对配方")

        token = _load_refresh_token(profile_id)
        auth_payload = dict(auth or {})
        auth_mode = str(auth_payload.get("mode") or "saved").strip().lower()
        if auth_mode not in {"saved", "qr", "credentials"}:
            raise SteamTradeupError("Steam 登录方式无效")
        account_name = str(auth_payload.get("account_name") or "").strip()
        password = str(auth_payload.get("password") or "")
        guard_code = str(auth_payload.get("guard_code") or "").strip()
        if auth_mode == "credentials" and (
            not account_name or not password or not guard_code
        ):
            raise SteamTradeupError("请输入 Steam 登录账号、密码和 Steam Guard 令牌")
        command = [str(_node_executable()), str(_bridge_script())]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise SteamTradeupError("无法启动本地一键汰换组件") from exc
        with self._lock:
            self._process = process
            cancelled = self._cancelled
        if cancelled:
            self.cancel()
            raise SteamTradeupCancelledError("已取消一键汰换")

        request = {
            "refreshToken": token,
            "authMode": auth_mode,
            "accountName": account_name,
            "password": password,
            "steamGuardCode": guard_code,
            "profileDataDir": str(profile_session_root(profile_id) / "steam_gc_data"),
            "expectedSteamId": expected_steam_id,
            "assetIds": asset_ids,
        }
        assert process.stdin is not None
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.close()

        result: dict[str, Any] | None = None
        error_message = "一键汰换未返回结果"
        error_code = ""
        assert process.stdout is not None
        for raw_line in process.stdout:
            try:
                event = json.loads(raw_line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "")
            if event_name == "token":
                received_steam_id = str(event.get("steamId") or "")
                refresh_token = str(event.pop("refreshToken", "") or "")
                if refresh_token and (
                    not received_steam_id or received_steam_id == expected_steam_id
                ):
                    try:
                        _save_refresh_token(
                            profile_id,
                            refresh_token,
                            expected_steam_id,
                        )
                    except OSError:
                        # Authorization still works for this run; the user can
                        # scan again next time if Windows cannot persist DPAPI.
                        pass
                continue
            if event_name == "crafting":
                with self._lock:
                    self._irreversible = True
            elif event_name == "success":
                result = event
            elif event_name == "error":
                error_message = str(event.get("message") or error_message)
                error_code = str(event.get("code") or "")
            if on_event is not None:
                on_event(dict(event))

        exit_code = process.wait()
        with self._lock:
            self._process = None
            cancelled = self._cancelled
        if cancelled and not self._irreversible:
            raise SteamTradeupCancelledError("已取消一键汰换")
        if result is None or exit_code != 0:
            if error_code in {"ACCOUNT_MISMATCH", "STEAM_AUTH"}:
                clear_tradeup_session(profile_id)
            raise SteamTradeupError(error_message, error_code)
        return result
