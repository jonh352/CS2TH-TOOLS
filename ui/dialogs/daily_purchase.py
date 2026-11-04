"""选择收货账号，自动使用当天的采购记录。"""
from PySide6.QtWidgets import QInputDialog

from core.inventory_steam_accounts import get_active_profile_id, list_profile_entries, load_steam_account_config_dict
from core.purchase_batches import daily_purchase_batch
from core.purchase_tracking import load_profile_inventory_items
from ui.widgets.toast import show_toast


def choose_daily_purchase(parent):
    accounts = [entry for entry in list_profile_entries() if entry.get("id")]
    if not accounts:
        show_toast(parent, "请先在 Steam 库存添加收货账号", style="warning")
        return None
    labels = [str(entry.get("display_name") or "Steam") for entry in accounts]
    active = get_active_profile_id()
    current = next((i for i, entry in enumerate(accounts) if entry["id"] == active), 0)
    label, accepted = QInputDialog.getItem(parent, "加入采购", "选择收货账号（自动归入今天）：", labels, current, False)
    if not accepted:
        return None
    profile = str(accounts[labels.index(label)]["id"])
    cfg = load_steam_account_config_dict(profile)
    try:
        return daily_purchase_batch(profile_id=profile, steam_id=str(cfg.get("steam_id") or ""),
                                    account_name=label, inventory_items=load_profile_inventory_items(profile))
    except (OSError, ValueError) as exc:
        show_toast(parent, f"加入采购失败：{exc}", style="warning")
        return None
