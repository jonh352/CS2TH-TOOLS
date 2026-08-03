"""Scrape exact-wear market listings for material-collection → alchemy import."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event

from PySide6.QtCore import QThread, Signal

from core.alchemy_quality import get_name_map, normalize_name
from core.collection_cancel import CollectionCancelled
from core.market_access_session import close_access_sessions
from core.market_candidates import fetch_exact_wear_candidates


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


def collect_candidates_parallel(
    *,
    materials: list[dict],
    providers: list[str],
    provider_intervals: dict[str, int],
    progress,
    cancel_check,
    silent: bool = False,
) -> tuple[list[dict], list[str]]:
    """Collect providers concurrently while keeping each provider rate-limited."""
    name_map = get_name_map()

    def collect_provider(provider: str) -> tuple[list[dict], list[str]]:
        provider_rows: list[dict] = []
        provider_errors: list[str] = []
        try:
            for material in materials:
                if cancel_check():
                    break
                name = str(material.get("name") or "").strip()
                template = name_map.get(normalize_name(name))
                if template is None:
                    provider_errors.append(f"{provider}：无法匹配材料 {name}")
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
                            request_interval=float(
                                max(1, provider_intervals.get(provider, 2))
                            ),
                            progress=progress,
                            extra_ids=_extra_ids_for_provider(provider, material),
                            cancel_check=cancel_check,
                            silent=silent,
                            unit_price_cny=material.get("unit_price_cny"),
                        )
                    )
                except CollectionCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep other materials going
                    provider_errors.append(f"{provider}·{name}：{exc}")
            progress(f"{provider} 已收集 {len(provider_rows)} 条候选挂单")
        except CollectionCancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - report per-provider failure
            provider_errors.append(f"{provider}：{exc}")
        finally:
            # Browser sessions are thread-affine; close them in the same
            # provider thread in which they may have been created.
            if provider in {"c5", "eco"}:
                close_access_sessions(provider)
        return provider_rows, provider_errors

    ordered_providers = list(dict.fromkeys(providers))
    results: dict[str, tuple[list[dict], list[str]]] = {}
    if ordered_providers:
        executor = ThreadPoolExecutor(
            max_workers=min(4, len(ordered_providers)),
            thread_name_prefix="market-collection",
        )
        futures = {
            executor.submit(collect_provider, provider): provider
            for provider in ordered_providers
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
                        results[provider] = ([], [])
                        continue
                    try:
                        results[provider] = future.result()
                    except Exception as exc:  # defensive boundary
                        results[provider] = ([], [f"{provider}：{exc}"])
                if cancel_check():
                    for future in pending:
                        future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    candidates: list[dict] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for provider in ordered_providers:
        rows, provider_errors = results.get(provider, ([], []))
        errors.extend(provider_errors)
        for row in rows:
            key = (
                str(row.get("platform") or ""),
                str(row.get("listing_id") or row.get("goods_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(row)
    close_access_sessions("c5", "eco")
    return candidates, errors


class MaterialCollectionWorker(QThread):
    progress = Signal(str)
    completed = Signal(object, str)

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
        candidates, errors = collect_candidates_parallel(
            materials=self.materials,
            providers=self.providers,
            provider_intervals=self.provider_intervals,
            progress=self.progress.emit,
            cancel_check=self._is_stop_requested,
            silent=self.silent,
        )

        message = "；".join(errors)
        if self._is_stop_requested():
            message = ("已停止；" + message).strip("；")
        self.completed.emit(candidates, message)
