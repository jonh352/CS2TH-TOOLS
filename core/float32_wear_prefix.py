"""CS2 单精度磨损：按用户输入的十进制前缀求 [lo, hi) 内可表示的 float32 极值。"""

from __future__ import annotations

import re
import struct
from fractions import Fraction

_ONE_BITS = 0x3F800000  # float32(1.0) 的 bit pattern


def _bits_to_f32(bits: int) -> float:
    b = bits & 0xFFFFFFFF
    return struct.unpack(">f", struct.pack(">I", b))[0]


def _bits_to_fraction(bits: int) -> Fraction:
    exp = (bits >> 23) & 0xFF
    frac = bits & 0x7FFFFF

    if exp == 0:
        return Fraction(frac, 1 << 149)

    significand = (1 << 23) + frac
    shift = exp - 150
    if shift >= 0:
        return Fraction(significand << shift, 1)
    return Fraction(significand, 1 << (-shift))


def _first_bits_ge(x: Fraction) -> int:
    lo, hi = 0, _ONE_BITS
    while lo < hi:
        mid = (lo + hi) // 2
        if _bits_to_fraction(mid) < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def find_float32_range(s: str) -> tuple[float | None, float | None]:
    """
    若 ``s`` 为 ``0 <= s <= 1`` 的普通十进制字符串（与游戏磨损同域），且小数点后位数为 ``n``
    （无小数点时视为整数位步长 1），则求所有满足 ``value in [s, s + 10^-n)`` 的 float32 中
    最小与最大的可表示值（闭区间 [a, b]，b 为区间内最大 float32）。

    返回 ``(a, b)`` 为 Python ``float``；若无此类 float32 则 ``(None, None)``。
    """
    if not re.fullmatch(r"(?:0|1)(?:\.\d+)?", s):
        raise ValueError(
            "s 须为普通十进制字符串，例如 '0.12'、'0.7'、'1.0'，且整数部分仅为 0 或 1"
        )

    lo = Fraction(s)
    if not (0 <= lo <= 1):
        raise ValueError("s 须满足 0 <= s <= 1")

    if "." in s:
        frac_part = s.split(".", 1)[1]
        n = len(frac_part)
        step = Fraction(1, 10**n)
    else:
        step = Fraction(1, 1)

    hi = lo + step

    left = _first_bits_ge(lo)
    right_exclusive = _ONE_BITS + 1 if hi > 1 else _first_bits_ge(hi)

    if left >= right_exclusive:
        return None, None

    a = _bits_to_f32(left)
    b = _bits_to_f32(right_exclusive - 1)
    return a, b


def find_float32_range_intersection(
    s: str,
    min_wear: float | None = None,
    max_wear: float | None = None,
) -> tuple[float | None, float | None, str | None]:
    """按十进制前缀求 float32 区间，并可与皮肤磨损范围求交。

    返回 ``(lo, hi, err)``；成功时 ``err`` 为 ``None``，且当提供模板区间时会先裁剪到
    ``[min_wear, max_wear]``。若该前缀在 CS2 中无可表示的 float32，或与模板区间无交集，
    则返回中文错误消息。
    """
    raw = (s or "").strip().replace("。", ".")
    try:
        lo_opt, hi_opt = find_float32_range(raw)
    except ValueError:
        return None, None, "请输入有效的数字"
    if lo_opt is None or hi_opt is None:
        return None, None, f"很遗憾，CS2中不存在以{raw}开头的磨损度"

    lo = float(lo_opt)
    hi = float(hi_opt)
    if min_wear is not None and max_wear is not None:
        tmpl_lo = float(min_wear)
        tmpl_hi = float(max_wear)
        if lo > tmpl_hi or hi < tmpl_lo:
            return None, None, "目标磨损超出该目标产物的磨损区间"
        lo = max(lo, tmpl_lo)
        hi = min(hi, tmpl_hi)
    if lo > hi:
        return None, None, "目标磨损超出该目标产物的磨损区间"
    return lo, hi, None
