"""Persist ECO submission status against the exact recipe account and assets."""

from datetime import datetime, timezone


def publication_identity(batch, entry):
    return {
        "profile_id": str(batch.get("profile_id") or ""),
        "steam_id": str(batch.get("steam_id") or ""),
        "asset_ids": sorted(str(row.get("matched_assetid") or "") for row in entry.get("materials") or [] if isinstance(row, dict)),
    }


def recipe_publication(batch, entry):
    saved = entry.get("eco_publication")
    identity = publication_identity(batch, entry)
    if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in identity.items()):
        return {}
    return saved if saved.get("state") in {"listed", "submitted", "pending", "uncertain"} else {}


def furnace_is_published(row):
    state = row.get("HL_FurnaceState", row.get("FurnaceState"))
    return (state in (200, 300, 301, 350, 400) and row.get("CanPayment") is not True) or (
        state == 100 and row.get("CanPayment") is False
        and row.get("IsMatching") is True and row.get("IsFurnaceUser") is True
    )


def sync_furnace_publications(rows):
    """Confirm only exact locally bound furnaces; absence never resets a record."""
    from core import purchase_batches as purchases
    confirmed = {str(row.get("FurnaceID")): row for row in rows if row.get("FurnaceID") and furnace_is_published(row)}
    if not confirmed:
        return 0
    updated = 0
    for path, batch in purchases.list_purchase_batches():
        changed = False
        for entry in batch.get("recipes") or []:
            saved = recipe_publication(batch, entry)
            row = confirmed.get(saved.get("furnace_id"))
            if row is None or saved.get("state") == "listed":
                continue
            saved.update(state="listed", source="eco_confirmed", platform_state=row.get("HL_FurnaceState", row.get("FurnaceState")), updated_at=datetime.now(timezone.utc).isoformat())
            changed = True
            updated += 1
        if changed:
            purchases._write_batch(path, batch)
    return updated


def record_recipe_publication(path, entry_id, state, *, payload=None, furnace_id=""):
    from core import purchase_batches as purchases
    batch = purchases.load_purchase_batch(path)
    entry = next((row for row in batch.get("recipes") or [] if str(row.get("id")) == str(entry_id)), None)
    if entry is None:
        raise ValueError("来源配方已不存在")
    if state == "unlisted":
        entry.pop("eco_publication", None)
    else:
        if state not in {"listed", "submitted", "pending", "uncertain"}:
            raise ValueError("无效的合炉状态")
        identity = publication_identity(batch, entry)
        if len(identity["asset_ids"]) not in (5, 10) or "" in identity["asset_ids"]:
            raise ValueError("配方材料尚未完整匹配，不能标记合炉状态")
        if payload is not None:
            assets = sorted(str(row.get("AssetId") or "") for row in payload.get("AssetList") or [])
            if str(payload.get("ReceiveSteamID") or "") != identity["steam_id"] or assets != identity["asset_ids"]:
                raise ValueError("提交的账号或材料已改变，不能关联到该来源配方")
        previous = recipe_publication(batch, entry)
        keep_confirmed = state == "submitted" and previous.get("source") == "eco_confirmed" and previous.get("furnace_id") == furnace_id
        entry["eco_publication"] = {
            **identity, "state": state, "furnace_id": str(furnace_id or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": "eco_request" if payload is not None else "manual",
        }
        if payload is not None and payload.get("_material_listing"):
            entry["eco_publication"]["material_listing"] = payload["_material_listing"]
        if keep_confirmed:
            entry["eco_publication"].update(state="listed", source="eco_confirmed", platform_state=previous.get("platform_state"))
    purchases._write_batch(path, batch)
