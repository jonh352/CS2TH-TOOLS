"""Read CS2 craft results from Steam Community inventory history."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from config import STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S
from core.inventory_steam_accounts import profile_session_root
from core.steam.browser_session import PLAYWRIGHT_PROFILE_LOCK
from core.steam.constants import STEAM_APP_ID
from core.steam.launch import launch_ephemeral_chromium_context
from core.steam_session_profiles import steam_account_storage_state_path


_HISTORY_URL = "https://steamcommunity.com/my/inventoryhistory/"
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class SteamInventoryHistoryError(RuntimeError):
    pass


def _storage_state_is_usable(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    return isinstance(cookies, list) and any(
        isinstance(row, dict)
        and row.get("name") == "steamLoginSecure"
        and "steamcommunity.com" in str(row.get("domain") or "")
        for row in cookies
    )


def _history_ajax_url(cursor: dict[str, Any]) -> str:
    params: list[tuple[str, object]] = [
        ("ajax", "1"), ("app[]", str(STEAM_APP_ID)), ("l", "english")
    ]
    for key in ("time", "time_frac", "s"):
        if cursor.get(key) not in (None, ""):
            params.append((f"cursor[{key}]", cursor[key]))
    return f"{_HISTORY_URL}?{urlencode(params)}"


def _description_index(value: object) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}

    def visit(node: object) -> None:
        if isinstance(node, dict):
            class_id = str(node.get("classid") or "")
            if class_id:
                app_id = str(node.get("appid") or STEAM_APP_ID)
                instance_id = str(node.get("instanceid") or "0")
                result[(app_id, class_id, instance_id)] = dict(node)
                result.setdefault((app_id, class_id, "0"), dict(node))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return result


def _economy_identity(raw: str) -> tuple[str, str, str] | None:
    numbers = re.findall(r"\d+", str(raw or ""))
    if len(numbers) < 2:
        return None
    if str(STEAM_APP_ID) in numbers:
        app_pos = numbers.index(str(STEAM_APP_ID))
        tail = numbers[app_pos + 1 :]
        if len(tail) >= 3:
            return str(STEAM_APP_ID), tail[-2], tail[-1]
    return str(STEAM_APP_ID), numbers[-2], numbers[-1]


class _HistoryHtmlParser(HTMLParser):
    def __init__(self, descriptions: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        super().__init__(convert_charrefs=True)
        self._descriptions = descriptions
        self.rows: list[dict[str, Any]] = []
        self._depth = 0
        self._row: dict[str, Any] | None = None
        self._sign = ""
        self._sign_depth = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: str(value or "") for key, value in attrs}
        classes = set(attr.get("class", "").split())
        if self._row is None and "tradehistoryrow" in classes:
            self._row = {"text": [], "inputs": [], "outputs": []}
            self._depth = 1
            return
        if self._row is None:
            return
        if tag not in _VOID_TAGS:
            self._depth += 1
        if "tradehistory_items_plusminus" in classes:
            self._sign_depth = self._depth
        economy_raw = attr.get("data-economy-item", "")
        direct_class_id = attr.get("data-classid", "")
        is_item = bool(economy_raw or direct_class_id or "history_item" in classes)
        if is_item:
            identity = (
                (
                    str(attr.get("data-appid") or STEAM_APP_ID),
                    direct_class_id,
                    str(attr.get("data-instanceid") or "0"),
                )
                if direct_class_id
                else _economy_identity(economy_raw or attr.get("id", ""))
            )
            description = dict(self._descriptions.get(identity or ("", "", ""), {}))
            if identity:
                description.setdefault("appid", identity[0])
                description.setdefault("classid", identity[1])
                description.setdefault("instanceid", identity[2])
            description.setdefault("name", attr.get("title", ""))
            description["history_element_id"] = attr.get("id", "")
            target = "outputs" if self._sign == "+" else "inputs" if self._sign == "-" else ""
            if target:
                self._row[target].append(description)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        text = str(data or "").strip()
        if not text:
            return
        self._row["text"].append(text)
        if self._sign_depth >= 0 and text in {"+", "-", "＋", "－"}:
            self._sign = "+" if text in {"+", "＋"} else "-"

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if self._sign_depth == self._depth:
            self._sign_depth = -1
        self._depth -= 1
        if self._depth > 0:
            return
        row = self._row
        row["text"] = " ".join(row["text"])
        if row["inputs"] and row["outputs"]:
            self.rows.append(row)
        self._row = None
        self._sign = ""
        self._sign_depth = -1


def parse_steam_inventory_history_page(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parser = _HistoryHtmlParser(_description_index(payload.get("descriptions")))
    parser.feed(str(payload.get("html") or ""))
    return [
        row for row in parser.rows
        if len(row.get("inputs") or []) in (5, 10) and len(row.get("outputs") or []) >= 1
    ]


def fetch_steam_tradeup_history(
    profile_id: str,
    *,
    max_pages: int = 10,
    on_status: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch Crafted rows through the same browser session used by inventory.

    A plain HTTP request is insufficient for some Steam sessions: Community can
    redirect through ``login.steampowered.com`` and refresh ``steamLoginSecure``
    in browser JavaScript.  Restoring Playwright storage here preserves that SSO
    round-trip and then uses the browser context's authenticated request client.
    """
    profile_id = str(profile_id or "").strip()
    state_path = steam_account_storage_state_path(profile_session_root(profile_id))
    if not _storage_state_is_usable(state_path):
        raise SteamInventoryHistoryError("该 Steam 账号尚未登录，请先到 Steam 库存页登录")
    def say(message: str) -> None:
        if on_status is not None:
            on_status(message)

    timeout_ms = min(
        30_000,
        max(1, int(float(STEAM_BROWSER_COMMUNITY_HOME_TIMEOUT_S) * 1000)),
    )
    with PLAYWRIGHT_PROFILE_LOCK:
        try:
            with sync_playwright() as playwright:
                browser = None
                context = None
                try:
                    say("正在恢复 Steam 社区登录…")
                    browser, context = launch_ephemeral_chromium_context(
                        playwright,
                        headless=True,
                        storage_state=state_path,
                    )
                    page = context.new_page()
                    say("正在打开 Steam 库存历史…")
                    first_response = page.goto(
                        _history_ajax_url({}),
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    if "/login" in str(page.url).lower():
                        try:
                            page.wait_for_url(
                                lambda url: "/login" not in str(url).lower(),
                                timeout=15_000,
                            )
                        except Exception:
                            pass
                    if "/login" in str(page.url).lower():
                        raise SteamInventoryHistoryError(
                            "Steam 社区登录仍未生效，请在登录窗口确认已进入个人资料页后再关闭"
                        )
                    # Persist any steamRefresh -> steamLoginSecure renewal that
                    # occurred while opening Community in the real browser.
                    context.storage_state(path=str(state_path))

                    cursor: dict[str, Any] = {}
                    events: list[dict[str, Any]] = []
                    for _page in range(max(1, int(max_pages))):
                        say(f"正在读取 Steam 合成记录（第 {_page + 1} 页）…")
                        response = first_response if _page == 0 else None
                        payload: object = None
                        for attempt in range(2):
                            if (
                                response is None
                                or "/login" in str(response.url or "").lower()
                            ):
                                response = context.request.get(
                                    _history_ajax_url(cursor),
                                    headers={"Accept": "application/json"},
                                    timeout=30_000,
                                )
                            response_url = str(response.url or "").lower()
                            if "/login" in response_url or response.status in (401, 403):
                                raise SteamInventoryHistoryError(
                                    "Steam 社区登录仍未生效，请在登录窗口确认已进入个人资料页后再关闭"
                                )
                            try:
                                payload = response.json()
                            except Exception:
                                payload = None
                            if isinstance(payload, dict) and bool(payload.get("success")):
                                break
                            if attempt == 0:
                                # Steam occasionally returns JSON null when many
                                # history pages are requested in quick succession.
                                page.wait_for_timeout(900)
                                response = None
                        if not isinstance(payload, dict) or not bool(payload.get("success")):
                            error = payload.get("error") if isinstance(payload, dict) else ""
                            raise SteamInventoryHistoryError(
                                str(error or "Steam 库存历史暂时无响应，请稍后重试")
                            )
                        events.extend(parse_steam_inventory_history_page(payload))
                        next_cursor = payload.get("cursor")
                        if (
                            not isinstance(next_cursor, dict)
                            or not next_cursor
                            or next_cursor == cursor
                        ):
                            break
                        cursor = next_cursor
                        page.wait_for_timeout(250)
                    return events
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
        except SteamInventoryHistoryError:
            raise
        except Exception as exc:
            raise SteamInventoryHistoryError(f"Steam 库存历史请求失败：{exc}") from exc
