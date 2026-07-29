"""炼金「特殊磨损」：产物真实平均磨损落在所选产物的给定上下界 [lo, hi]（闭区间）内时，求 Top 低成本解。

上下界由 UI 输入；映射到归一化后，在底物归一化磨损之和的区间上搜索（MITM 使用半开上界，
对 inclusive hi 加极小 epsilon）。另保留 ``mean_wear_interval_from_string`` 供旧式「小数前缀区间」解析。

仅使用 Meet-in-the-Middle（半集枚举 + 排序二分合并）；单层组合数过大或无可行解时直接结束。
以下搜索例程中 ``sum_hi`` 恒为「上界不含」。"""

from __future__ import annotations

import bisect
import heapq
import math
import os
import random
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    wait,
)
from typing import Any, Callable, Optional
import numpy as np
from .alchemy_calc import (
    ComputationCancelled,
    _break_even_rate_from_prob_map,
    _compute_recipe_inputs,
    _expectation_rate_from_prob_map,
    _product_prob_map_for_recipe_ui,
    _products_display_from_prob_map,
    _substrates_display,
    split_required_instance_pairs,
    transform_to_instances,
)
from .alchemy_quality import get_pid_map
from .data_utils import SkinInstance
from .process_pool_kill import shutdown_process_pool_hard

# 仅多进程 worker 子进程在 initializer 中赋值，避免每轮任务重复 pickle 大体量 dict
_sw_worker_selected_data: Optional[list[dict[str, Any]]] = None
_sw_worker_price_map: Optional[dict[str, Any]] = None

# 搜索内部：(已枚举叶数, C(n,k), DFS 结点数, UI 段内 pct, n_inst, k)
_InternalSwProgressCallback = Callable[[int, int, int, int, int, int], None]
# 再附加 (当前轮 0-based, 总轮数)；n_inst/k 供 UI 按规模估算耗时
SpecialWearProgressCallback = Callable[[int, int, int, int, int, int, int, int], None]

_MAX_SUBSTRATES = 55
_MAX_SPECIAL_WEAR_ROUNDS = 50
_NUM_STRATA = 10
_PRICE_BIAS_EPS = 1e-6


def _future_result_poll(
    fut: Future,
    cancel_check: Optional[Callable[[], bool]],
) -> Any:
    """带 ``cancel_check`` 时用超时轮询 ``Future.result``，避免在单任务上无限阻塞而无法停止。"""
    if cancel_check is None:
        return fut.result()
    while True:
        if cancel_check():
            raise ComputationCancelled
        try:
            return fut.result(timeout=0.15)
        except TimeoutError:
            continue
_TOP_N = 10
# MITM：单侧「恰好选 t 件」的组合数超此值则放弃 MITM（不再尝试其它算法）
_MAX_MITM_SINGLE_LAYER = 5_500_000
# 真实磨损 [lo,hi] 闭区间映射到 MITM 半开上界时加的裕量（归一化和尺度）
_SUM_HI_INCLUSIVE_EPS = 1e-14


def estimate_special_wear_selection_upper_bound(
    selected_data: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """抽样前上界：``n_cap = min(有效底物对数, _MAX_SUBSTRATES)``，与 ``C(n_cap,k)``。

    供 UI 在后台线程尚未汇报首帧时立刻显示「按底物规模估算」的剩余时间。
    """
    inp = _compute_recipe_inputs(selected_data)
    if isinstance(inp, str):
        return 0, 0, 0
    _, optional_instances, required_instances, k = inp
    remaining_k = k - len(required_instances)
    n_optional = min(len(optional_instances), max(0, _MAX_SUBSTRATES - len(required_instances)))
    n = len(required_instances) + n_optional
    if remaining_k < 1 or n < k:
        return n, k, 0
    try:
        tc = math.comb(n_optional, remaining_k)
    except (OverflowError, ValueError):
        tc = 10**15
    return n, k, int(tc)


def estimate_special_wear_initial_eta_seconds(
    n_inst: int,
    k: int,
    total_combo: int,
    rounds: int,
) -> float:
    """按底物数 n、k 与理论组合数 C(n,k)（及并行轮数）启发式估算总墙钟秒数。

    实际 MITM 有强剪枝，真实耗时通常明显小于按 C(n,k) 线性外推；此处用次线性标度并设上下限。
    """
    if n_inst <= 0 or k <= 0 or total_combo <= 0:
        return 8.0
    tc = float(min(int(total_combo), 10**15))
    work = tc ** 0.38
    sec = 0.35 + work * 4.8e-5
    sec *= 1.0 + 0.12 * max(0, k - 3)
    sec *= 1.0 + 0.06 * max(0, n_inst - 25)
    # 与 compute_special_wear_recipes 多轮分支一致：并行 worker 有上限
    parallel = max(
        1, min(int(rounds), max(1, (os.cpu_count() or 4) // 2), 6)
    )
    if rounds > 1:
        # 墙钟 ≈ ceil(rounds / parallel) 波串行叠加；每波内并行，时长近似单轮 MITM
        waves = math.ceil(float(rounds) / float(parallel))
        sec *= float(waves) * 1.07
    return max(3.0, min(sec, 4 * 3600.0))


def estimate_special_wear_eta_interval_seconds(
    n_inst: int,
    k: int,
    total_combo: int,
    rounds: int,
) -> tuple[float, float]:
    """与 ``estimate_special_wear_initial_eta_seconds`` 同源的中点，向两侧拉开成区间（剪枝与进度非线性）。"""
    mid = estimate_special_wear_initial_eta_seconds(
        n_inst, k, total_combo, rounds
    )
    if n_inst <= 0 or k <= 0 or total_combo <= 0:
        return 4.0, max(6.0, mid * 1.5)
    lo = max(2.0, mid * 0.38)
    hi = min(4 * 3600.0, mid * 2.45)
    if lo > hi:
        lo, hi = hi * 0.35, hi
    return lo, hi


def validate_wear_string(s: str) -> tuple[bool, str]:
    """校验磨损字符串：可解析为有限浮点数即可（无小数位数限制）。"""
    raw = (s or "").strip().replace("。", ".")
    if not raw:
        return False, "请输入磨损数值"
    try:
        v = float(raw)
    except ValueError:
        return False, "请输入有效的数字"
    if math.isnan(v) or math.isinf(v):
        return False, "请输入有限数值"
    return True, ""


def _recipe_from_solution(
    solution: list[SkinInstance],
    k: int,
    price_map: dict,
) -> dict[str, Any]:
    cost = sum(s.price for s in solution)
    # 与主模式 _finalize_recipes_from_rate_results 一致：底物归一化磨损算术平均
    nfvs = [s.normalized_value for s in solution]
    avg_nfv = sum(nfvs) / len(nfvs)
    product_probs = _product_prob_map_for_recipe_ui(solution, k, avg_nfv, price_map)
    expectation, rate = _expectation_rate_from_prob_map(product_probs, cost)
    break_even_rate = _break_even_rate_from_prob_map(product_probs, cost)
    products_display = _products_display_from_prob_map(product_probs)
    substrates_display = _substrates_display(solution)

    return {
        "cost": cost,
        "expectation": expectation,
        "rate": rate,
        "avg_nfv": float(avg_nfv),
        "break_even_rate": break_even_rate,
        "substrates_display": substrates_display,
        "products_display": products_display,
    }


def _build_one_sw_recipe_task(
    payload: tuple[list[int], list[int], list[SkinInstance], list[SkinInstance], int, dict, str],
) -> dict[str, Any]:
    """供进程池：从 raw_results 一条构造 UI recipe（与单轮并行构建共用）。"""
    idxs, order, instances, required_instances, k, price_map, meta_s = payload
    inv_order = [order[j] for j in idxs]
    solution = [*required_instances, *[instances[i] for i in inv_order]]
    r = _recipe_from_solution(solution, k, price_map)
    r["special_wear_input"] = meta_s
    return r


def _min_sum_segment(fs: list[float], lo: int, hi: int, r: int) -> float:
    """有序 fs 上区间 [lo, hi) 内取 r 件的最小和；hi 为开区间。"""
    if r == 0:
        return 0
    if lo + r > hi:
        return float("inf")
    return sum(fs[lo + i] for i in range(r))


def _max_sum_segment(fs: list[float], lo: int, hi: int, r: int) -> float:
    if r == 0:
        return 0
    if hi - lo < r:
        return float("-inf")
    return sum(fs[hi - r + i] for i in range(r))


def _mitm_cap_exceeded(n: int, k: int, n0: int) -> bool:
    lo_t = max(0, k - (n - n0))
    hi_t = min(k, n0)
    for t in range(lo_t, hi_t + 1):
        try:
            if math.comb(n0, t) > _MAX_MITM_SINGLE_LAYER:
                return True
            if math.comb(n - n0, k - t) > _MAX_MITM_SINGLE_LAYER:
                return True
        except (OverflowError, ValueError):
            return True
    return False


def _enumerate_half_mitm(
    fs: list[float],
    costs: list[float],
    seg_lo: int,
    seg_hi: int,
    pick: int,
    o_lo: int,
    o_hi: int,
    o_pick: int,
    sum_lo: float,
    sum_hi: float,
    nodes: list[int],
    leaves: list[int],
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[tuple[float, float, int]]:
    """枚举本半恰好 pick 件；剪枝用另一半 o_pick 件的最小/最大和。mask 为全局位。"""
    if pick == 0:
        return [(0, 0.0, 0)]
    out: list[tuple[float, float, int]] = []
    span = seg_hi - seg_lo
    if span < pick:
        return out

    def dfs(pos: int, rem: int, ssum: float, cst: float, msk: int) -> None:
        nodes[0] += 1
        if (
            cancel_check is not None
            and nodes[0] % 4096 == 0
            and cancel_check()
        ):
            raise ComputationCancelled
        if rem == 0:
            leaves[0] += 1
            out.append((ssum, cst, msk))
            return
        if pos >= seg_hi:
            return
        if seg_hi - pos < rem:
            return
        mn_t = _min_sum_segment(fs, pos, seg_hi, rem)
        mx_t = _max_sum_segment(fs, pos, seg_hi, rem)
        mn_o = _min_sum_segment(fs, o_lo, o_hi, o_pick) if o_pick > 0 else 0
        mx_o = _max_sum_segment(fs, o_lo, o_hi, o_pick) if o_pick > 0 else 0
        if ssum + mn_t + mn_o >= sum_hi:
            return
        if ssum + mx_t + mx_o < sum_lo:
            return
        dfs(pos + 1, rem, ssum, cst, msk)
        dfs(pos + 1, rem - 1, ssum + fs[pos], cst + costs[pos], msk | (1 << pos))

    dfs(seg_lo, pick, 0, 0.0, 0)
    return out


def _mitm_merge_to_heap(
    left_list: list[tuple[float, float, int]],
    right_list: list[tuple[float, float, int]],
    sum_lo: float,
    sum_hi: float,
    heap: list[tuple[float, int]],
    top_n: int,
    pairs: list[int],
) -> None:
    if not left_list or not right_list:
        return
    right_list.sort(key=lambda x: x[0])
    sums_r = [x[0] for x in right_list]
    for sL, cL, mL in left_list:
        lo_b = sum_lo - sL
        hi_b = sum_hi - sL
        i0 = bisect.bisect_left(sums_r, lo_b)
        # sum_hi 为开上界：须 sL + sR < sum_hi 即 sR < hi_b
        i1 = bisect.bisect_left(sums_r, hi_b)
        pairs[0] += max(0, i1 - i0)
        for j in range(i0, i1):
            sR, cR, mR = right_list[j]
            tc = cL + cR
            fm = mL | mR
            if len(heap) < top_n:
                heapq.heappush(heap, (-tc, fm))
            elif tc < -heap[0][0]:
                heapq.heapreplace(heap, (-tc, fm))


def _search_top_cheap_mitm(
    fs: list[float],
    costs: list[float],
    k: int,
    sum_lo: float,
    sum_hi: float,
    top_n: int,
    *,
    total_combo: int,
    progress_callback: Optional[_InternalSwProgressCallback] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[Optional[list[tuple[float, list[int]]]], int, int]:
    """
    Meet-in-the-Middle：n 分成两半，枚举两侧恰 t / k-t 件后按和区间二分合并。
    无法做（规模过大）返回 (None, _, _)；无解放 []。
    """
    n = len(fs)
    if k <= 0 or k > n:
        return [], 0, 0
    n0 = n // 2
    # if _mitm_cap_exceeded(n, k, n0):
    #     return None, 0, 0

    nodes = [0]
    leaves = [0]
    pairs = [0]
    h_best: list[tuple[float, int]] = []

    ts = [
        t
        for t in range(max(0, k - (n - n0)), min(k, n0) + 1)
        if 0 <= k - t <= n - n0
    ]
    if not ts:
        return [], 0, 0

    def emit(pct: int) -> None:
        if progress_callback is None:
            return
        progress_callback(leaves[0], total_combo, nodes[0], pct, n, k)

    emit(0)
    for idx, t_left in enumerate(ts):
        if cancel_check is not None and cancel_check():
            raise ComputationCancelled
        t_right = k - t_left
        left_list = _enumerate_half_mitm(
            fs,
            costs,
            0,
            n0,
            t_left,
            n0,
            n,
            t_right,
            sum_lo,
            sum_hi,
            nodes,
            leaves,
            cancel_check=cancel_check,
        )
        right_list = _enumerate_half_mitm(
            fs,
            costs,
            n0,
            n,
            t_right,
            0,
            n0,
            t_left,
            sum_lo,
            sum_hi,
            nodes,
            leaves,
            cancel_check=cancel_check,
        )
        _mitm_merge_to_heap(
            left_list, right_list, sum_lo, sum_hi, h_best, top_n, pairs,
        )
        emit(min(85, int(85 * (idx + 1) / len(ts))))

    if not h_best:
        return [], leaves[0], nodes[0]

    ordered = sorted(((-tup[0], tup[1]) for tup in h_best), key=lambda x: x[0])
    out: list[tuple[float, list[int]]] = []
    seen_m: set[int] = set()
    for cst, msk in ordered:
        if msk in seen_m:
            continue
        seen_m.add(msk)
        idxs = [j for j in range(n) if (msk >> j) & 1]
        out.append((cst, idxs))
        if len(out) >= top_n:
            break

    if progress_callback is not None:
        progress_callback(leaves[0], total_combo, nodes[0], 85, n, k)
    return out, leaves[0], nodes[0]


def _search_top_cheap(
    fs: list[float],
    costs: list[float],
    k: int,
    sum_lo: float,
    sum_hi: float,
    top_n: int,
    *,
    total_combo: int,
    progress_callback: Optional[_InternalSwProgressCallback] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[list[tuple[float, list[int]]], int, int]:
    """仅 Meet-in-the-Middle；单层过大或无可行解时返回空列表。"""
    mitm_r, m_cc, m_nn = _search_top_cheap_mitm(
        fs,
        costs,
        k,
        sum_lo,
        sum_hi,
        top_n,
        total_combo=total_combo,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    if mitm_r is None:
        return [], m_cc, m_nn
    return mitm_r, m_cc, m_nn


def _substrate_row_key(d: dict) -> tuple:
    return (d.get("goods_name"), d.get("float_value"), d.get("platform"))


def _weighted_sample_indices_no_replace(
    weights: list[float], k: int, rng: random.Random
) -> list[int]:
    """Gumbel-max 技巧：按权重无放回抽样 k 个下标。"""
    if k <= 0:
        return []
    n = len(weights)
    if k >= n:
        return list(range(n))
    keys = [
        -math.log(max(rng.random(), 1e-300)) / (weights[i] + _PRICE_BIAS_EPS)
        for i in range(n)
    ]
    order = sorted(range(n), key=lambda i: keys[i])
    return order[:k]


def sample_substrates_stratified_price_biased(
    pairs: list[tuple[dict, SkinInstance]],
    *,
    rng: random.Random,
    max_n: int = _MAX_SUBSTRATES,
    num_strata: int = _NUM_STRATA,
) -> list[dict]:
    """
    按归一化磨损分层，层内按价格倒数加权无放回抽样，不足 max_n 时用全局价格偏置补齐。
    pairs 须为 (原始 dict, SkinInstance)，且与 transform_to_instances 规则一致。
    """
    if not pairs:
        return []
    if len(pairs) <= max_n:
        return [d for d, _ in pairs]

    pairs_sorted = sorted(pairs, key=lambda x: x[1].normalized_value)
    m = len(pairs_sorted)
    b = max(1, min(num_strata, max_n))
    base_q = max_n // b
    rem = max_n % b
    quotas = [base_q + (1 if i < rem else 0) for i in range(b)]

    selected_keys: set[tuple] = set()
    out: list[dict] = []

    for bi in range(b):
        lo = (bi * m) // b
        hi = ((bi + 1) * m) // b
        bucket = pairs_sorted[lo:hi]
        quota = quotas[bi]
        if not bucket or quota <= 0:
            continue
        if len(bucket) <= quota:
            picks = bucket
        else:
            weights = [1.0 / (inst.price + _PRICE_BIAS_EPS) for _, inst in bucket]
            idxs = _weighted_sample_indices_no_replace(weights, quota, rng)
            picks = [bucket[i] for i in idxs]
        for d, _inst in picks:
            key = _substrate_row_key(d)
            if key not in selected_keys:
                selected_keys.add(key)
                out.append(d)

    if len(out) < max_n:
        remaining = [(d, inst) for d, inst in pairs if _substrate_row_key(d) not in selected_keys]
        need = max_n - len(out)
        if remaining and need > 0:
            weights = [1.0 / (inst.price + _PRICE_BIAS_EPS) for _, inst in remaining]
            take = min(need, len(remaining))
            idxs = _weighted_sample_indices_no_replace(weights, take, rng)
            for i in idxs:
                d, _inst = remaining[i]
                key = _substrate_row_key(d)
                if key not in selected_keys:
                    selected_keys.add(key)
                    out.append(d)

    return out[:max_n]


def _recipe_fingerprint(recipe: dict) -> tuple:
    subs = recipe.get("substrates_display") or []
    return tuple(
        sorted((s.get("name"), s.get("float_value"), s.get("platform")) for s in subs)
    )


def _merge_recipe_list_into_best(best: dict[tuple, dict], recipes: list[dict]) -> None:
    """增量合并，避免先 list.extend 再整表去重时峰值翻倍。"""
    for r in recipes:
        fp = _recipe_fingerprint(r)
        c = float(r.get("cost", 0))
        if fp not in best or c < float(best[fp].get("cost", 0)):
            best[fp] = r


def _sw_init_worker(selected_data: list[dict[str, Any]], price_map: dict[str, Any]) -> None:
    global _sw_worker_selected_data, _sw_worker_price_map
    _sw_worker_selected_data = selected_data
    _sw_worker_price_map = price_map


def _sw_round_worker_task(
    args: tuple[int, int, int, str, str, str],
) -> tuple[int, list[dict[str, Any]], int, int, int, int]:
    """
    子进程内执行一轮：分层抽样 + MITM + 组装配方（无进度回调）。
    依赖 _sw_init_worker 已注入底物与 price_map，本任务只传小元组。
    返回 (r_idx, recipes, cc, total_combo, nn, n_inst)。
    """
    r_idx, base_seed, k, sum_lo_s, sum_hi_s, meta_s = args
    selected_data = _sw_worker_selected_data
    price_map = _sw_worker_price_map
    if not selected_data or price_map is None:
        return r_idx, [], 0, 0, 0, 0

    sum_lo = sum_lo_s
    sum_hi = sum_hi_s

    inp = _compute_recipe_inputs(selected_data)
    if isinstance(inp, str):
        return r_idx, [], 0, 0, 0, 0
    _, _optional_instances, required_instances, k = inp
    remaining_k = k - len(required_instances)
    if remaining_k < 1:
        return r_idx, [], 0, 0, 0, 0
    req_nfv_total = sum(inst.normalized_value for inst in required_instances)
    sum_lo -= req_nfv_total
    sum_hi -= req_nfv_total
    if sum_lo >= sum_hi:
        return r_idx, [], 0, 0, 0, len(required_instances)
    required_pairs, optional_pairs = split_required_instance_pairs(selected_data)
    if len(optional_pairs) < remaining_k:
        return r_idx, [], 0, 0, 0, len(required_instances)

    rr = random.Random((base_seed + r_idx * 100003) % (2**31 - 1))
    sampled_optional = sample_substrates_stratified_price_biased(
        optional_pairs,
        rng=rr,
        max_n=max(remaining_k, _MAX_SUBSTRATES - len(required_pairs)),
    )
    del required_pairs, optional_pairs
    instances = transform_to_instances(sampled_optional)
    del sampled_optional
    if not instances or len(instances) < remaining_k:
        return r_idx, [], 0, 0, 0, 0

    order = sorted(range(len(instances)), key=lambda i: instances[i].normalized_value)
    fs = [instances[i].normalized_value for i in order]
    costs = [instances[i].price for i in order]

    n_inst = len(required_instances) + len(instances)
    try:
        total_combo = math.comb(len(instances), remaining_k) if len(instances) >= remaining_k >= 0 else 0
    except (OverflowError, ValueError):
        total_combo = 0

    raw_results, _cc, _nn = _search_top_cheap(
        fs,
        costs,
        remaining_k,
        sum_lo,
        sum_hi,
        _TOP_N,
        total_combo=total_combo,
        progress_callback=None,
    )
    if not raw_results:
        return r_idx, [], _cc, total_combo, _nn, n_inst

    tasks: list[tuple[list[int], list[int], list[SkinInstance], list[SkinInstance], int, dict, str]] = [
        (idxs, order, instances, required_instances, k, price_map, meta_s)
        for _cost, idxs in raw_results
    ]
    del raw_results, fs, costs
    recipes = [_build_one_sw_recipe_task(t) for t in tasks]
    del tasks
    return r_idx, recipes, _cc, total_combo, _nn, n_inst


def _scale_special_wear_progress(
    progress_callback: Optional[SpecialWearProgressCallback],
    round_idx: int,
    total_rounds: int,
    n_inst: int,
    k: int,
) -> Optional[_InternalSwProgressCallback]:
    if progress_callback is None:
        return None

    def inner(
        checked: int, total: int, nodes: int, pct: int, ni: int, kj: int
    ) -> None:
        lo = int(100 * round_idx / total_rounds)
        hi = int(100 * (round_idx + 1) / total_rounds)
        mapped = lo + int((hi - lo) * max(0, min(100, pct)) / 100)
        mapped = min(100, mapped)
        progress_callback(
            checked, total, nodes, mapped, round_idx, total_rounds, ni, kj
        )

    return inner


def compute_special_wear_recipes(
    selected_data: list[dict],
    price_map: dict,
    target_paint_index: str,
    target_wear_lo: float,
    target_wear_hi: float,
    *,
    rng: Optional[random.Random] = None,
    progress_callback: Optional[SpecialWearProgressCallback] = None,
    rounds: int = 1,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    分层抽样（按归一化磨损）+ 层内价格偏置（价低更易被抽中），再至多 50 条底物上做搜索；
    产物平均真实磨损须在所选产物的 [target_wear_lo, target_wear_hi]（闭区间）内。
    可设多轮（rounds），每轮独立抽样，各轮结果按底物集合去重后合并取成本最低至多 10 组。
    各轮统一走多进程轮任务路径（rounds=1 时也使用 1 个 worker 进程），保持执行逻辑一致。
    返回 (recipes_for_ui, error_msg)。
    """
    if rng is None:
        rng = random.Random()
    rounds = max(1, min(int(rounds), _MAX_SPECIAL_WEAR_ROUNDS))
    if cancel_check is not None and cancel_check():
        raise ComputationCancelled

    pid_map = get_pid_map()
    out_tpl = pid_map.get(str(target_paint_index))
    if not out_tpl:
        return [], "未找到指定产物模板"

    inp = _compute_recipe_inputs(selected_data)
    if isinstance(inp, str):
        return [], inp
    _, optional_instances, required_instances, k = inp
    remaining_k = k - len(required_instances)
    if remaining_k < 1:
        return [], f"必选底物最多只能选择 {k - 1} 个"
    if len(optional_instances) < remaining_k:
        return [], "非必选底物数量不足，无法补足配方"

    try:
        lo = target_wear_lo
        hi = target_wear_hi
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        return [], "磨损上下界无效"
    if lo > hi:
        return [], "最小磨损不能大于最大磨损"

    min_f = out_tpl.min_float
    max_f = out_tpl.max_float
    if lo < min_f:
        return [], f"目标磨损下界 {lo} 低于该产物最低磨损 {min_f}"
    if hi > max_f:
        return [], f"目标磨损上界 {hi} 高于该产物最高磨损 {max_f}"
    span = max_f - min_f
    if span <= 0:
        return [], "产物模板磨损区间无效"

    nfv_lo = (lo - min_f) / span
    nfv_hi = (hi - min_f) / span
    sum_lo = k * nfv_lo
    # MITM 上界为开区间：对闭区间 [lo,hi] 的 hi 端加极小量以包含上界
    sum_hi = k * nfv_hi + _SUM_HI_INCLUSIVE_EPS
    if sum_lo >= sum_hi:
        return [], "目标磨损区间在归一化尺度下退化无效，请检查产物与输入"

    meta_s = f"{target_paint_index}:{lo}:{hi}"
    best_by_fp: dict[tuple, dict] = {}
    last_cc, last_tc, last_nn = 0, 0, 0
    last_n_inst = 0

    base_seed = rng.getrandbits(31)
    # 轮数大时勿按轮数拉满进程数：Windows spawn + 大体量 pickle 易导致整机卡顿
    _cpu_half = max(1, (os.cpu_count() or 4) // 2)
    max_workers = max(1, min(int(rounds), _cpu_half, 6))
    task_tpls = [
        (r_idx, base_seed, k, sum_lo, sum_hi, meta_s)
        for r_idx in range(rounds)
    ]
    completed = 0
    ex = ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_sw_init_worker,
        initargs=(selected_data, price_map),
    )
    hard_cancel_mp = False
    try:
        futs = [ex.submit(_sw_round_worker_task, t) for t in task_tpls]
        del task_tpls
        pending_mp = set(futs)
        while pending_mp:
            if cancel_check is not None and cancel_check():
                hard_cancel_mp = True
                shutdown_process_pool_hard(ex)
                raise ComputationCancelled
            done_mp, pending_mp = wait(
                pending_mp, timeout=0.12, return_when=FIRST_COMPLETED
            )
            for fut in done_mp:
                try:
                    r_idx, recipes, cc, tc, nn, n_inst_mp = (
                        _future_result_poll(fut, cancel_check)
                    )
                except ComputationCancelled:
                    hard_cancel_mp = True
                    shutdown_process_pool_hard(ex)
                    raise
                _merge_recipe_list_into_best(best_by_fp, recipes)
                del recipes
                last_cc, last_tc, last_nn = cc, tc, nn
                last_n_inst = n_inst_mp
                completed += 1
                if progress_callback is not None:
                    progress_callback(
                        cc,
                        tc,
                        nn,
                        min(99, int(100 * completed / rounds)),
                        r_idx,
                        rounds,
                        n_inst_mp,
                        k,
                    )
    finally:
        if not hard_cancel_mp:
            ex.shutdown(wait=True)

    if not best_by_fp:
        return [], "未找到满足目标产物磨损的组合（可尝试增加搜索轮数或扩大底物池）"

    out = sorted(best_by_fp.values(), key=lambda x: float(x.get("cost", 0)))[:_TOP_N]
    if progress_callback is not None:
        progress_callback(
            last_cc, last_tc, last_nn, 100, rounds - 1, rounds, last_n_inst, k
        )
    return out, None
