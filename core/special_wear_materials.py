"""Map a target product float32 interval to valid input material intervals."""

from __future__ import annotations

import bisect

from core.data_utils import MID_VALUE_LIST, SkinTemplate


def neighboring_purchase_interval(
    wear_value: float,
    *,
    min_float: float = 0.0,
    max_float: float = 1.0,
) -> tuple[float, float]:
    """Return the current MID bucket plus one bucket on either side."""
    value = max(float(min_float), min(float(max_float), float(wear_value)))
    upper_index = bisect.bisect_right(MID_VALUE_LIST, value)
    upper_index = max(1, min(upper_index, len(MID_VALUE_LIST) - 1))
    current_lower_index = upper_index - 1
    purchase_lower_index = max(0, current_lower_index - 1)
    purchase_upper_index = min(len(MID_VALUE_LIST) - 1, upper_index + 1)
    return (
        max(float(min_float), MID_VALUE_LIST[purchase_lower_index]),
        min(float(max_float), MID_VALUE_LIST[purchase_upper_index]),
    )


def build_special_wear_materials(
    target_template: SkinTemplate,
    *,
    target: float,
    target_low: float,
    target_high: float,
    pid_map: dict[str, SkinTemplate],
) -> list[dict]:
    normalized = SkinTemplate.float_to_normalized(
        target, target_template.min_float, target_template.max_float
    )
    normalized_low = SkinTemplate.float_to_normalized(
        target_low, target_template.min_float, target_template.max_float
    )
    normalized_high = SkinTemplate.float_to_normalized(
        target_high, target_template.min_float, target_template.max_float
    )
    materials: list[dict] = []
    for pid in target_template.lower_skins:
        lower = pid_map.get(str(pid))
        if lower is None:
            continue
        wear_value = SkinTemplate.normalized_to_float(
            normalized, lower.min_float, lower.max_float
        )
        low = SkinTemplate.normalized_to_float(
            normalized_low, lower.min_float, lower.max_float
        )
        high = SkinTemplate.normalized_to_float(
            normalized_high, lower.min_float, lower.max_float
        )
        purchase_low, purchase_high = neighboring_purchase_interval(
            wear_value,
            min_float=lower.min_float,
            max_float=lower.max_float,
        )
        name = (
            f"{lower.weapon_name} | {lower.skin_name}"
            if lower.skin_name
            else lower.weapon_name
        )
        materials.append(
            {
                "name": name,
                "wear_value": wear_value,
                "exact_min_wear": min(low, high),
                "exact_max_wear": max(low, high),
                "min_wear": purchase_low,
                "max_wear": purchase_high,
            }
        )
    return materials
