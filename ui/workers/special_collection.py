"""Collect exact-wear listings and solve a concrete special-wear basket."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.alchemy_quality import get_name_map, normalize_name
from core.alchemy_special_wear import compute_special_wear_recipes
from core.market_candidates import fetch_exact_wear_candidates


class SpecialCollectionWorker(QThread):
    progress = Signal(str)
    completed = Signal(object, object, str)

    def __init__(
        self,
        *,
        materials: list[dict],
        providers: list[str],
        provider_intervals: dict[str, int],
        target_paint_index: str,
        target_wear_low: float,
        target_wear_high: float,
        slot_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.materials = [dict(item) for item in materials]
        self.providers = list(providers)
        self.provider_intervals = dict(provider_intervals)
        self.target_paint_index = str(target_paint_index)
        self.target_wear_low = float(target_wear_low)
        self.target_wear_high = float(target_wear_high)
        self.slot_count = 5 if int(slot_count) == 5 else 10

    def run(self) -> None:
        candidates: list[dict] = []
        errors: list[str] = []
        seen: set[tuple[str, str]] = set()
        name_map = get_name_map()
        for provider in self.providers:
            provider_count = 0
            try:
                for material in self.materials:
                    name = str(material.get("name") or "").strip()
                    template = name_map.get(normalize_name(name))
                    if template is None:
                        errors.append(f"{provider}：无法匹配材料 {name}")
                        continue
                    rows = fetch_exact_wear_candidates(
                        provider,
                        template=template,
                        display_name=name,
                        min_wear=float(material.get("min_wear") or 0),
                        max_wear=float(material.get("max_wear") or 1),
                        max_pages=2,
                        request_interval=float(
                            max(1, self.provider_intervals.get(provider, 5))
                        ),
                        progress=self.progress.emit,
                    )
                    for row in rows:
                        key = (
                            str(row.get("platform") or ""),
                            str(row.get("listing_id") or row.get("goods_id") or ""),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append(row)
                        provider_count += 1
                self.progress.emit(f"{provider} 已收集 {provider_count} 条候选挂单")
            except Exception as exc:  # noqa: BLE001 - report per-provider failure
                errors.append(f"{provider}：{exc}")

        if len(candidates) < self.slot_count:
            detail = "；".join(errors)
            message = (
                f"有效候选只有 {len(candidates)} 件，"
                f"至少需要 {self.slot_count} 件"
            )
            if detail:
                message += f"；{detail}"
            self.completed.emit(candidates, [], message)
            return

        self.progress.emit(
            f"候选池共 {len(candidates)} 件，"
            f"正在组合最省成本的{self.slot_count}件…"
        )
        try:
            recipes, error = compute_special_wear_recipes(
                candidates,
                {},
                self.target_paint_index,
                self.target_wear_low,
                self.target_wear_high,
                rounds=3,
            )
        except Exception as exc:  # noqa: BLE001 - keep the GUI worker alive
            self.completed.emit(
                candidates,
                [],
                f"组合计算失败：{exc}",
            )
            return
        message = str(error or "")
        if errors:
            suffix = "；".join(errors)
            message = f"{message}；{suffix}".strip("；")
        self.completed.emit(candidates, recipes, message)
