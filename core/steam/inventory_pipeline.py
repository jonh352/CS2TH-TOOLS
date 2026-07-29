"""库存 JSON 解析、展示字段与拉取编排。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from core.inventory_hide_names import (
    is_inventory_item_hidden_by_name_list,
    load_inventory_hide_name_set,
)

from .browser_session import get_steam_cookies
from .errors import SteamInventoryFetchCancelledError, SteamSessionExpiredError
from .inventory_api import request_steam_inventory_context
from .models import SteamWebProfile

_STEAM_MARKET_LISTED_VALUE_CN = (
    "⇆ 该物品已在 Steam 社区市场挂售，挂售期间不可消耗或修改。"
)
# 中文「格林尼治时间」日期块：YYYY 年 M月 [Steam 曾多写一个「月」] D 日 (H:M:S)
# ``(?:\s*月)*`` 在修 bug 后为 0 次匹配，不影响「4月 29 日」等正常文案。
_RE_ZH_GMT_CLOCK_DATETIME = (
    r"(\d{4})\s*年\s*(\d{1,2})月(?:\s*月)*\s*(\d{1,2})\s*日?\s*"
    r"\(\s*(\d{1,2})\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})\s*\)"
)
# 新格式（context 2 等）：「在 格林尼治时间2026 年…(7:00:00) 后可交易…」；「在」与「格林尼治」之间可能有空格。
_RE_TRADE_PROTECT_AT_GMT_TIME_CN = re.compile(
    r"在\s*格林尼治时间\s*" + _RE_ZH_GMT_CLOCK_DATETIME,
    re.UNICODE,
)
# 无前缀「在」：「…交易冷却期 格林尼治时间2026 年…」等。
_RE_TRADE_PROTECT_INLINE_GMT_TIME_CN = re.compile(
    r"格林尼治时间\s*" + _RE_ZH_GMT_CLOCK_DATETIME,
    re.UNICODE,
)
# 旧格式：「在 2026 4月 29 (12:34:56) 格林尼治…」
_RE_TRADE_PROTECT_UNTIL_GMT_CN = re.compile(
    r"在\s*(\d{4})\s+(\d{1,2})月(?:\s*月)*\s*(\d{1,2})\s*日?\s*"
    r"\(\s*(\d{1,2})\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})\s*\)\s*格林尼治",
    re.UNICODE,
)

_TRADE_HOLD_END_PATTERNS: tuple[re.Pattern[str], ...] = (
    _RE_TRADE_PROTECT_AT_GMT_TIME_CN,
    _RE_TRADE_PROTECT_INLINE_GMT_TIME_CN,
    _RE_TRADE_PROTECT_UNTIL_GMT_CN,
)
# 2026+ Steam 简中：「在7 月 1 日 下午 3:00之前不能被消耗、改造或转让」（l=schinese，本地时区）
_RE_TRADE_PROTECT_LOCAL_ZH_CN = re.compile(
    r"在\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
    r"(上午|下午)\s*"
    r"(\d{1,2})\s*:\s*(\d{1,2})\s*"
    r"之前",
    re.UNICODE,
)
# context 2 非黄盾冷却：「在 7 月 5 日 上午 12:00 后可交易/可在市场上出售」或内嵌 [date]unix[/date]
_RE_TRADE_AVAILABLE_AFTER_LOCAL_ZH_CN = re.compile(
    r"在\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
    r"(上午|下午)\s*"
    r"(\d{1,2})\s*:\s*(\d{1,2})\s*"
    r"后",
    re.UNICODE,
)
_RE_DATE_TAG_UNIX = re.compile(r"\[date\](\d{10,})\[/date\]", re.UNICODE)
_STEAM_INVENTORY_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _zh_ampm_to_hour24(ampm: str, hour: int) -> int:
    if ampm == "下午":
        return hour if hour == 12 else hour + 12
    return 0 if hour == 12 else hour


def _infer_trade_hold_year(
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    now_dt: datetime,
) -> int:
    """owner_description 无年份时，按当前时刻推断（冷却结束点通常在未来数日内）。"""
    now_ts = now_dt.timestamp()
    for year in (now_dt.year, now_dt.year + 1):
        try:
            dt = datetime(
                year, month, day, hour, minute, 0, tzinfo=_STEAM_INVENTORY_LOCAL_TZ
            )
        except ValueError:
            continue
        if dt.timestamp() > now_ts - 86400.0:
            return year
    return now_dt.year


def _trade_hold_end_timestamp_from_gmt_text(val: str) -> float | None:
    """格林尼治时间旧文案 → UTC unix 时间戳。"""
    for pat in _TRADE_HOLD_END_PATTERNS:
        m = pat.search(val)
        if not m:
            continue
        y, mo, d, h, mi, s = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        except ValueError:
            continue
        return dt.timestamp()
    return None


def _trade_hold_end_timestamp_from_date_tag(val: str) -> float | None:
    """``[date]1783180800[/date]`` 内嵌 unix 时间戳（Steam 部分文案仍保留该标签）。"""
    m = _RE_DATE_TAG_UNIX.search(val)
    if not m:
        return None
    try:
        ts = float(m.group(1))
    except ValueError:
        return None
    return ts if ts > 0 else None


def _local_zh_datetime_from_match(
    m: re.Match[str],
    *,
    now: float | None = None,
) -> float | None:
    month, day = int(m.group(1)), int(m.group(2))
    hour = _zh_ampm_to_hour24(m.group(3), int(m.group(4)))
    minute = int(m.group(5))
    now_dt = datetime.fromtimestamp(
        time.time() if now is None else now, tz=_STEAM_INVENTORY_LOCAL_TZ
    )
    year = _infer_trade_hold_year(month, day, hour, minute, now_dt=now_dt)
    try:
        dt = datetime(
            year, month, day, hour, minute, 0, tzinfo=_STEAM_INVENTORY_LOCAL_TZ
        )
    except ValueError:
        return None
    return dt.timestamp()


def _trade_hold_end_timestamp_from_local_zh_text(
    val: str,
    *,
    now: float | None = None,
) -> float | None:
    """简中本地日期文案（Asia/Shanghai）→ unix 时间戳。"""
    m = _RE_TRADE_PROTECT_LOCAL_ZH_CN.search(val)
    if m:
        return _local_zh_datetime_from_match(m, now=now)
    m = _RE_TRADE_AVAILABLE_AFTER_LOCAL_ZH_CN.search(val)
    if m and ("可交易" in val or "市场上出售" in val):
        return _local_zh_datetime_from_match(m, now=now)
    return None


def _trade_hold_end_timestamp_from_text(val: str) -> float | None:
    """从 owner_description 文案中解析交易保护结束时刻；无法解析则 None。"""
    return (
        _trade_hold_end_timestamp_from_gmt_text(val)
        or _trade_hold_end_timestamp_from_date_tag(val)
        or _trade_hold_end_timestamp_from_local_zh_text(val)
    )


def get_wear_level(float_val: float | None) -> str:
    if float_val is None:
        return "Unknown"
    if float_val < 0.07:
        return "Factory New"
    if float_val < 0.15:
        return "Minimal Wear"
    if float_val < 0.38:
        return "Field-Tested"
    if float_val < 0.45:
        return "Well-Worn"
    return "Battle-Scarred"


def parse_owner_cooldown_meta(owner_descriptions: list | None) -> dict:
    """从描述条目中解析挂售/交易冷却；遍历全部 value，避免文案不在固定下标时漏判。"""
    if not owner_descriptions:
        return {"cooldown_kind": None, "cooldown_ends_at": None}
    for od in owner_descriptions:
        if not isinstance(od, dict):
            continue
        val = str(od.get("value") or "").strip()
        if not val:
            continue
        if val == _STEAM_MARKET_LISTED_VALUE_CN or (
            "Steam 社区市场挂售" in val and "挂售" in val
        ):
            return {"cooldown_kind": "market_listed", "cooldown_ends_at": None}
        ends_ts = _trade_hold_end_timestamp_from_text(val)
        if ends_ts is not None:
            return {"cooldown_kind": "trade_hold", "cooldown_ends_at": ends_ts}
    return {"cooldown_kind": None, "cooldown_ends_at": None}


def format_cooldown_remaining(ends_at_unix: float, *, now: float | None = None) -> str:
    t = time.time() if now is None else now
    remaining = float(ends_at_unix) - t
    if remaining <= 0:
        return "可出售"
    day_sec = 86400.0
    hour_sec = 3600.0
    if remaining > 24 * hour_sec:
        days = int(remaining // day_sec)
        hours = int((remaining % day_sec) // hour_sec)
        return f"{days}天{hours}小时"
    h = int(remaining // hour_sec)
    if h > 0:
        return f"{h}小时"
    minutes = max(1, int(remaining // 60))
    return f"{minutes}分钟"


def format_inventory_status_line(item: dict) -> str:
    if item.get("marketable"):
        return "可出售"
    k = item.get("cooldown_kind")
    if k == "market_listed":
        return "Steam在售中"
    ends = item.get("cooldown_ends_at")
    if k == "trade_hold" and ends is not None:
        return format_cooldown_remaining(float(ends))
    return "冷却中"


def _normalize_steam_tag_color_hex(raw: object) -> str:
    """Steam ``tags[].color`` 多为 6 位十六进制无 ``#``，返回 ``#rrggbb`` 或空串。"""
    s = str(raw or "").strip().lstrip("#")
    if len(s) != 6:
        return ""
    for c in s:
        if c not in "0123456789abcdefABCDEF":
            return ""
    return "#" + s.lower()


def steam_rarity_from_tags(tags: list | None) -> tuple[str, str]:
    """
    从 ``descriptions[].tags`` 取品质标签。
    返回 ``(rarity_suffix, rarity_color)``：后缀为 ``internal_name`` 去掉 ``Rarity_`` 后小写；
    ``rarity_color`` 为 ``#rrggbb``（来自同一条 tag 的 ``color``），无则 ``""``。
    """
    if not tags:
        return "", ""
    for t in tags:
        if not isinstance(t, dict):
            continue
        internal = str(t.get("internal_name") or "").strip()
        if not internal.startswith("Rarity_"):
            continue
        cat = str(t.get("category") or "").strip()
        if cat and cat != "Rarity":
            continue
        suffix = internal[len("Rarity_") :].lower()
        color_hex = _normalize_steam_tag_color_hex(t.get("color"))
        return suffix, color_hex
    return "", ""


def build_desc_details(inventory: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for d in inventory.get("descriptions") or []:
        try:
            cid = d.get("classid")
            iid = d.get("instanceid")
            if cid is None or iid is None:
                continue
            key = f"{cid}_{iid}"
        except (TypeError, ValueError):
            continue
        mhn = d.get("market_hash_name") or ""
        raw_tags = d.get("tags")
        tags_list = raw_tags if isinstance(raw_tags, list) else []
        result[key] = {
            "market_hash_name": mhn,
            "market_name": (d.get("market_name") or "").strip(),
            "name": (d.get("name") or "").strip(),
            "icon_url": (d.get("icon_url") or "").strip(),
            "owner_descriptions": d.get("owner_descriptions") or [],
            "tags": tags_list,
        }
    return result


def build_float_map(inventory: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for ap in inventory.get("asset_properties") or []:
        float_val = None
        paintseed = None
        for prop in ap.get("asset_properties") or []:
            if prop.get("propertyid") == 2 and prop.get("float_value") is not None:
                float_val = float(prop["float_value"])
            if prop.get("propertyid") == 1 and prop.get("int_value") is not None:
                paintseed = int(prop["int_value"])
        if float_val is not None:
            result[str(ap["assetid"])] = {"float": float_val, "paintseed": paintseed}
    return result


def inventory_item_visible_on_page(
    item: dict,
    *,
    exclude_names: frozenset[str] | None = None,
) -> bool:
    if item.get("float") is not None:
        visible = True
    elif "★" in (item.get("name") or ""):
        visible = True
    else:
        return False
    excl = exclude_names if exclude_names is not None else load_inventory_hide_name_set()
    if is_inventory_item_hidden_by_name_list(item, excl):
        return False
    return visible


def _effective_marketable_flag(marketable_param: int, cd_meta: dict) -> int:
    """context_id=2 仍会出现「某日 GMT 后可交易」等说明，不能一律标为可出售。"""
    if marketable_param != 1:
        return marketable_param
    k = cd_meta.get("cooldown_kind")
    ends = cd_meta.get("cooldown_ends_at")
    if k == "market_listed":
        return 0
    if k == "trade_hold" and ends is not None:
        if time.time() < float(ends):
            return 0
    return 1


def process_inventory(
    inventory: dict | None,
    marketable: int,
    *,
    steam_inventory_context_id: int | None = None,
) -> list[dict]:
    if not inventory:
        return []
    assets = inventory.get("assets") or []
    float_map = build_float_map(inventory)
    desc_details = build_desc_details(inventory)
    results: list[dict] = []
    for asset in assets:
        data = float_map.get(str(asset["assetid"]))
        float_val = data["float"] if data else None
        key = f"{asset['classid']}_{asset['instanceid']}"
        detail = desc_details.get(key) or {}
        mhn = (detail.get("market_hash_name") or "").strip()
        market_hash_name = mhn if mhn else None
        market_name = (detail.get("market_name") or "").strip()
        name = (detail.get("name") or "").strip()
        icon_url = (detail.get("icon_url") or "").strip()
        od_list = detail.get("owner_descriptions")
        if not isinstance(od_list, list):
            od_list = []
        cd_meta = parse_owner_cooldown_meta(od_list)
        eff_m = _effective_marketable_flag(marketable, cd_meta)
        tags_raw = detail.get("tags")
        rarity_suffix, rarity_color = steam_rarity_from_tags(
            tags_raw if isinstance(tags_raw, list) else None
        )
        row: dict = {
            "assetid": str(asset["assetid"]),
            "market_hash_name": market_hash_name,
            "market_name": market_name,
            "name": name,
            "icon_url": icon_url,
            "float": float_val,
            "paintseed": data.get("paintseed") if data else None,
            "wear": get_wear_level(float_val),
            "marketable": eff_m,
            "cooldown_kind": cd_meta["cooldown_kind"],
            "cooldown_ends_at": cd_meta["cooldown_ends_at"],
        }
        if rarity_suffix:
            row["rarity"] = rarity_suffix
        if rarity_color:
            row["rarity_color"] = rarity_color
        if steam_inventory_context_id is not None:
            row["steam_inventory_context_id"] = steam_inventory_context_id
        results.append(row)
    excl = load_inventory_hide_name_set()
    return [r for r in results if inventory_item_visible_on_page(r, exclude_names=excl)]


def _raise_if_inventory_fetch_cancelled(
    cancel_check: Optional[Callable[[], bool]],
) -> None:
    if cancel_check is not None and cancel_check():
        raise SteamInventoryFetchCancelledError()


def fetch_inventory(
    session_dir: Path | str | None = None,
    on_status: Optional[Callable[[str], None]] = None,
    *,
    allow_interactive_login: bool = False,
    known_steam_id: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[list[dict], SteamWebProfile]:
    _raise_if_inventory_fetch_cancelled(cancel_check)
    cookie, profile = get_steam_cookies(
        session_dir=session_dir,
        on_status=on_status,
        allow_interactive_login=allow_interactive_login,
        known_steam_id=known_steam_id,
    )
    if cookie is None or profile is None:
        if allow_interactive_login:
            raise RuntimeError(
                "无法获取 Steam 会话，请先点击「登录 Steam」完成登录后再获取库存"
            )
        raise SteamSessionExpiredError()

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    sid = profile.steam_id
    _raise_if_inventory_fetch_cancelled(cancel_check)
    _status("正在请求冷却中物品…")
    cooldown = request_steam_inventory_context(cookie, 16, steam_id=sid)
    cooldown_results = process_inventory(
        cooldown, 0, steam_inventory_context_id=16
    )
    _raise_if_inventory_fetch_cancelled(cancel_check)
    _status("正在请求可出售物品…")
    sellable = request_steam_inventory_context(cookie, 2, steam_id=sid)
    sellable_results = process_inventory(
        sellable, 1, steam_inventory_context_id=2
    )
    return cooldown_results + sellable_results, profile
