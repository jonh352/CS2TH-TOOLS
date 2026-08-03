"""Steam login and inventory workers."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.steam import (
    STEAM_BROWSER_NOT_INSTALLED_MSG,
    STEAM_FETCH_SESSION_EXPIRED,
    SteamBrowserLaunchError,
    SteamBrowserNotFoundError,
    SteamInventoryFetchCancelledError,
    SteamSessionExpiredError,
    fetch_inventory,
    is_network_error,
    login_steam_session,
)


def _profile_payload(profile) -> dict[str, str]:
    return {
        "steam_id": profile.steam_id,
        "personaname": profile.personaname,
        "avatar_path": profile.avatar_local_path or "",
        "avatar_url": profile.avatar_url or "",
    }


class SteamLoginWorker(QThread):
    status = Signal(str)
    completed = Signal(object, str)

    def __init__(self, session_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.session_dir = session_dir

    def run(self) -> None:
        try:
            profile = login_steam_session(self.session_dir, self.status.emit)
            self.completed.emit(_profile_payload(profile), "")
        except SteamBrowserNotFoundError:
            self.completed.emit(None, STEAM_BROWSER_NOT_INSTALLED_MSG)
        except SteamBrowserLaunchError as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, "请检查网络连接" if is_network_error(exc) else str(exc))


class InventoryFetchWorker(QThread):
    status = Signal(str)
    completed = Signal(object, object, str, str)

    def __init__(self, session_dir: Path, known_steam_id: str, parent=None) -> None:
        super().__init__(parent)
        self.session_dir = session_dir
        self.known_steam_id = known_steam_id

    def run(self) -> None:
        try:
            items, profile = fetch_inventory(
                session_dir=self.session_dir,
                on_status=self.status.emit,
                allow_interactive_login=False,
                known_steam_id=self.known_steam_id,
                cancel_check=self.isInterruptionRequested,
            )
            price_status = ""
            try:
                self.status.emit("库存获取完成，正在同步最新价格…")
                from core.alchemy_calc import (
                    apply_inventory_buff_prices,
                    build_price_map,
                    load_product_price_raw,
                )

                raw = load_product_price_raw(force_remote=True)
                items, matched = apply_inventory_buff_prices(
                    items,
                    build_price_map(raw),
                    str(raw.get("fetch_time") or ""),
                )
                price_status = f"价格已同步，匹配 {matched}/{len(items)} 件"
            except Exception as exc:
                # Steam inventory is still useful when the price service is
                # temporarily unavailable, so do not turn this into a fetch
                # failure or discard the freshly downloaded inventory.
                price_status = f"库存已更新；价格同步失败：{exc}"
            self.completed.emit(items, _profile_payload(profile), "", price_status)
        except SteamSessionExpiredError:
            self.completed.emit(None, None, STEAM_FETCH_SESSION_EXPIRED, "")
        except SteamInventoryFetchCancelledError:
            self.completed.emit(None, None, "", "")
        except SteamBrowserNotFoundError:
            self.completed.emit(None, None, STEAM_BROWSER_NOT_INSTALLED_MSG, "")
        except SteamBrowserLaunchError as exc:
            self.completed.emit(None, None, str(exc), "")
        except Exception as exc:
            message = "请检查网络连接" if is_network_error(exc) else str(exc)
            self.completed.emit(None, None, message, "")
