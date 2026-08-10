"""Scrape exact-wear market listings for material-collection → alchemy import."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from threading import Event, Lock
from typing import Callable

from PySide6.QtCore import QThread, Signal

from core.alchemy_quality import get_name_map, normalize_name
from core.collection_cancel import CollectionCancelled, interruptible_wait
from core.market_access_session import close_access_sessions
from core.c5_browser_collect import close_c5_browser_collector
from core.market_candidates import (
    C5PlatformPausedError,
    EcoPlatformPausedError,
    c5_signer_collection_scope,
    collection_jitter_wait_seconds,
    fetch_exact_wear_candidates,
    provider_display_name,
)

# Two-stage scheduling: lower peak concurrency vs four-way parallel.
_COLLECTION_WAVE_BUFF_YYYP = frozenset({"buff", "yyyp"})
_COLLECTION_WAVE_C5_ECO = frozenset({"c5", "eco"})

UnitProgress = Callable[[int, int], None]


def _extra_ids_for_provider(provider: str, material: dict) -> list[int]:
    """Prefer recipe/website IDs when present; fall back to template mapping only."""
    key_map = {
        "buff": ("buff_id", "goods_id"),
        "yyyp": ("yyyp_id", "youpin_id"),
        "c5": ("c5_id",),
        "eco": ("eco_id",),
    }
    values: list[int] = []
    for key in key_map.get(provider, ()):
        raw = material.get(key)
        if raw in (None, ""):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    return values


def _provider_collection_waves(providers: list[str]) -> list[list[str]]:
    """Split providers into waves: BUFF∥悠悠, then C5∥ECO (then any others)."""
    ordered = list(dict.fromkeys(providers))
    wave_buff_yyyp = [p for p in ordered if p in _COLLECTION_WAVE_BUFF_YYYP]
    wave_c5_eco = [p for p in ordered if p in _COLLECTION_WAVE_C5_ECO]
    other = [
        p
        for p in ordered
        if p not in _COLLECTION_WAVE_BUFF_YYYP and p not in _COLLECTION_WAVE_C5_ECO
    ]
    waves: list[list[str]] = []
    if wave_buff_yyyp:
        waves.append(wave_buff_yyyp)
    if wave_c5_eco:
        waves.append(wave_c5_eco)
    if other:
        waves.append(other)
    return waves


def _wear_dedupe_key(float_value: object) -> float | None:
    try:
        wear = float(float_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if wear < 0:
        return None
    return round(wear, 8)


def _row_price(row: dict) -> float | None:
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def dedupe_candidates_keep_cheapest(rows: list[dict]) -> list[dict]:
    """Drop cross-platform duplicates of the same skin + wear; keep lowest price.

    Listing-id duplicates within a platform are removed first. Then rows that
    share the same goods name and wear (8 d.p.) keep only the cheapest entry.
    """
    listing_seen: set[tuple[str, str]] = set()
    unique_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        listing_key = (
            str(row.get("platform") or ""),
            str(row.get("listing_id") or row.get("goods_id") or ""),
        )
        if listing_key[1] and listing_key in listing_seen:
            continue
        if listing_key[1]:
            listing_seen.add(listing_key)
        unique_rows.append(row)

    best_by_skin_wear: dict[tuple[str, float], dict] = {}
    passthrough: list[dict] = []
    for row in unique_rows:
        name = normalize_name(str(row.get("goods_name") or "").strip())
        wear = _wear_dedupe_key(row.get("float_value"))
        price = _row_price(row)
        if not name or wear is None or price is None:
            passthrough.append(row)
            continue
        key = (name, wear)
        current = best_by_skin_wear.get(key)
        if current is None:
            best_by_skin_wear[key] = row
            continue
        current_price = _row_price(current)
        if current_price is None or price < current_price:
            best_by_skin_wear[key] = row

    # Preserve first-seen order among winners / passthrough rows.
    winners = {id(row) for row in best_by_skin_wear.values()}
    winners.update(id(row) for row in passthrough)
    return [row for row in unique_rows if id(row) in winners]


def collect_candidates_parallel(
    *,
    materials: list[dict],
    providers: list[str],
    provider_intervals: dict[str, int],
    progress,
    cancel_check,
    silent: bool = False,
    unit_progress: UnitProgress | None = None,
) -> tuple[list[dict], list[str], dict[str, list[dict]]]:
    """Collect in waves: BUFF∥悠悠 first, then C5∥ECO (each provider still serial).

    Returns ``(candidates, errors, retry_by_provider)``.
    ``retry_by_provider`` stays empty for ``eco`` / ``c5`` (pause stops the platform
    for this run with no post-run retry queue).

    ``unit_progress(done, total)`` reports finished platform×material units
    (success, failure, or skipped after platform pause all count).
    """
    name_map = get_name_map()
    ordered_providers = list(dict.fromkeys(providers))
    material_count = len(materials)
    total_units = max(0, material_count * len(ordered_providers))
    progress_lock = Lock()
    units_done = 0

    def report_units(n: int = 1) -> None:
        nonlocal units_done
        if unit_progress is None or n <= 0 or total_units <= 0:
            return
        with progress_lock:
            units_done = min(total_units, units_done + int(n))
            current = units_done
        unit_progress(current, total_units)

    if unit_progress is not None and total_units > 0:
        unit_progress(0, total_units)

    def collect_provider(
        provider: str,
    ) -> tuple[list[dict], list[str], list[dict]]:
        provider_rows: list[dict] = []
        provider_errors: list[str] = []
        paused_retry: list[dict] = []
        # Match fetch_exact_wear_candidates floors: 5s C5, 3s others.
        interval_floor = 5.0 if provider == "c5" else 3.0
        request_interval = max(
            interval_floor,
            float(max(1, provider_intervals.get(provider, int(interval_floor)))),
        )
        signer_scope = (
            c5_signer_collection_scope()
            if provider == "c5"
            else nullcontext()
        )
        completed_in_provider = 0
        try:
            with signer_scope:
                for index, material in enumerate(materials):
                    if cancel_check():
                        break
                    if index > 0:
                        wait_s = (
                            collection_jitter_wait_seconds(request_interval)
                            if provider in {"c5", "eco", "buff", "yyyp"}
                            else float(request_interval)
                        )
                        interruptible_wait(wait_s, cancel_check)
                    name = str(material.get("name") or "").strip()
                    template = name_map.get(normalize_name(name))
                    if template is None:
                        provider_errors.append(f"{provider}：无法匹配材料 {name}")
                        report_units(1)
                        completed_in_provider += 1
                        continue
                    try:
                        provider_rows.extend(
                            fetch_exact_wear_candidates(
                                provider,
                                template=template,
                                display_name=name,
                                min_wear=float(material.get("min_wear") or 0),
                                max_wear=float(material.get("max_wear") or 1),
                                max_pages=0,
                                request_interval=request_interval,
                                progress=progress,
                                extra_ids=_extra_ids_for_provider(provider, material),
                                cancel_check=cancel_check,
                                silent=silent,
                                unit_price_cny=material.get("unit_price_cny"),
                            )
                        )
                        report_units(1)
                        completed_in_provider += 1
                    except CollectionCancelled:
                        raise
                    except (EcoPlatformPausedError, C5PlatformPausedError) as exc:
                        provider_errors.append(f"{provider}·{name}：{exc}")
                        report_units(1)
                        completed_in_provider += 1
                        remaining = material_count - index - 1
                        if remaining > 0:
                            report_units(remaining)
                            completed_in_provider += remaining
                        if provider in {"eco", "c5"} and progress:
                            progress(
                                f"{provider_display_name(provider)} · "
                                "本轮已停止该平台采集"
                            )
                        break
                    except Exception as exc:  # noqa: BLE001 - keep other materials going
                        provider_errors.append(f"{provider}·{name}：{exc}")
                        report_units(1)
                        completed_in_provider += 1
            if progress:
                progress(f"{provider} 已收集 {len(provider_rows)} 条候选挂单")
        except CollectionCancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - report per-provider failure
            provider_errors.append(f"{provider}：{exc}")
            remaining = material_count - completed_in_provider
            if remaining > 0:
                report_units(remaining)
                completed_in_provider += remaining
        finally:
            # Browser sessions are thread-affine; close them in the same
            # provider thread in which they may have been created.
            if provider in {"c5", "eco"}:
                close_access_sessions(provider)
            if provider == "c5":
                close_c5_browser_collector()
        return provider_rows, provider_errors, paused_retry

    def run_wave(
        wave: list[str],
    ) -> dict[str, tuple[list[dict], list[str], list[dict]]]:
        wave_results: dict[str, tuple[list[dict], list[str], list[dict]]] = {}
        if not wave or cancel_check():
            return wave_results
        executor = ThreadPoolExecutor(
            max_workers=min(2, len(wave)),
            thread_name_prefix="market-collection",
        )
        futures = {
            executor.submit(collect_provider, provider): provider for provider in wave
        }
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    provider = futures[future]
                    if future.cancelled():
                        wave_results[provider] = ([], [], [])
                        report_units(material_count)
                        continue
                    try:
                        wave_results[provider] = future.result()
                    except Exception as exc:  # defensive boundary
                        wave_results[provider] = ([], [f"{provider}：{exc}"], [])
                        report_units(material_count)
                if cancel_check():
                    for future in pending:
                        future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        return wave_results

    results: dict[str, tuple[list[dict], list[str], list[dict]]] = {}
    waves = _provider_collection_waves(ordered_providers)
    for wave_index, wave in enumerate(waves):
        if cancel_check():
            break
        if progress and wave_index > 0:
            names = "、".join(provider_display_name(p) for p in wave)
            progress(f"上一阶段完成，开始采集：{names}")
        results.update(run_wave(wave))

    candidates: list[dict] = []
    errors: list[str] = []
    retry_by_provider: dict[str, list[dict]] = {"eco": [], "c5": []}
    for provider in ordered_providers:
        rows, provider_errors, paused_retry = results.get(provider, ([], [], []))
        errors.extend(provider_errors)
        candidates.extend(row for row in rows if isinstance(row, dict))
        if provider in retry_by_provider:
            retry_by_provider[provider].extend(
                dict(item) for item in paused_retry if isinstance(item, dict)
            )
    candidates = dedupe_candidates_keep_cheapest(candidates)
    close_access_sessions("c5", "eco")
    close_c5_browser_collector()
    return candidates, errors, retry_by_provider


class MaterialCollectionWorker(QThread):
    progress = Signal(str)
    progress_units = Signal(int, int)
    completed = Signal(object, str, object)

    def __init__(
        self,
        *,
        materials: list[dict],
        providers: list[str],
        provider_intervals: dict[str, int],
        silent: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.materials = [dict(item) for item in materials]
        self.providers = list(providers)
        self.provider_intervals = dict(provider_intervals)
        self.silent = bool(silent)
        self._stop_event = Event()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()

    def _is_stop_requested(self) -> bool:
        return self._stop_event.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        candidates, errors, retry_by_provider = collect_candidates_parallel(
            materials=self.materials,
            providers=self.providers,
            provider_intervals=self.provider_intervals,
            progress=self.progress.emit,
            cancel_check=self._is_stop_requested,
            silent=self.silent,
            unit_progress=self.progress_units.emit,
        )

        message = "；".join(errors)
        if self._is_stop_requested():
            message = ("已停止；" + message).strip("；")
        self.completed.emit(candidates, message, retry_by_provider)
