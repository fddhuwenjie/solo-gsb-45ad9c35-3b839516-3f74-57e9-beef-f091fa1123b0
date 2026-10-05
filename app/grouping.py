"""Pre-selection: enumerate 3~6 series combinations and pick the best pack.

Tie-breaking (per the release rules):

1. maximise the limiting capacity of the pack (min over its cells);
2. on equal limiting capacity, lexicographically smallest sorted cell-ID
   tuple (standard tuple comparison — length differences never occur between
   equal-size packs, and larger size is never preferred on its own).
"""
from __future__ import annotations

from itertools import combinations
from typing import List, Optional, Sequence, Tuple

from .config import EPS


class Cell:
    __slots__ = ("id", "chemistry", "capacity", "resistance", "version")

    def __init__(self, id: str, chemistry: str, capacity: float,
                 resistance: float, version: str):
        self.id = id
        self.chemistry = chemistry
        self.capacity = capacity
        self.resistance = resistance
        self.version = version


def _spread_ok(combo: Sequence[Cell], max_cap_delta: float,
               max_res_delta: float) -> Tuple[bool, float, float]:
    caps = [c.capacity for c in combo]
    ress = [c.resistance for c in combo]
    cap_spread = max(caps) - min(caps)
    res_spread = max(ress) - min(ress)
    ok = (cap_spread <= max_cap_delta + EPS
          and res_spread <= max_res_delta + EPS)
    return ok, cap_spread, res_spread


def find_best(cells: Sequence[Cell], min_size: int, max_size: int,
              min_capacity: float, max_cap_delta: float,
              max_res_delta: float) -> Optional[Tuple[List[Cell], float, float, float]]:
    """Return ``(combo, limiting_capacity, cap_spread, res_spread)`` or None."""
    eligible = [c for c in cells if c.capacity + EPS >= min_capacity]

    best: Optional[Tuple[List[Cell], float, float, float]] = None
    best_key = None
    for size in range(min_size, max_size + 1):
        if size > len(eligible):
            break
        for combo in combinations(eligible, size):
            ok, cap_spread, res_spread = _spread_ok(
                combo, max_cap_delta, max_res_delta)
            if not ok:
                continue
            limiting = min(c.capacity for c in combo)
            key = (-limiting, tuple(sorted(c.id for c in combo)))
            if best_key is None or key < best_key:
                best_key = key
                best = (list(combo), limiting, cap_spread, res_spread)
    return best


def diagnose(cells: Sequence[Cell], min_size: int, max_size: int,
             min_capacity: float, max_cap_delta: float,
             max_res_delta: float) -> Tuple[str, str]:
    """Explain why ``find_best`` returned None.

    Returns ``(reason_code, human_readable_reason)``.
    """
    if not cells:
        return (
            "ALL_OCCUPIED",
            "该化学体系下没有未占用电芯；可能已被已确认组占用，或尚未录入",
        )

    eligible = [c for c in cells if c.capacity + EPS >= min_capacity]
    if not eligible:
        cap_min = min(c.capacity for c in cells)
        cap_max = max(c.capacity for c in cells)
        return (
            "MIN_CAPACITY_UNREACHABLE",
            f"未占用电芯容量均低于最低单芯容量 {min_capacity:g} Ah"
            f"（现有 {cap_min:g}~{cap_max:g} Ah）",
        )

    if len(eligible) < min_size:
        return (
            "NOT_ENOUGH_CELLS",
            f"满足最低容量的未占用电芯仅 {len(eligible)} 节，"
            f"少于最少串联节数 {min_size}",
        )

    # Cells exist in workable quantities; check each spread constraint alone.
    cap_ok_any = False
    res_ok_any = False
    best_cap_spread = None
    best_res_spread = None
    for size in range(min_size, min(max_size, len(eligible)) + 1):
        for combo in combinations(eligible, size):
            _, cap_spread, res_spread = _spread_ok(combo, float("inf"), float("inf"))
            if best_cap_spread is None or cap_spread < best_cap_spread:
                best_cap_spread = cap_spread
            if best_res_spread is None or res_spread < best_res_spread:
                best_res_spread = res_spread
            if cap_spread <= max_cap_delta + EPS:
                cap_ok_any = True
            if res_spread <= max_res_delta + EPS:
                res_ok_any = True

    if not cap_ok_any:
        return (
            "CAPACITY_SPREAD_EXCEEDED",
            f"任意 {min_size}~{max_size} 节组合的容量差都超过上限 "
            f"{max_cap_delta:g} Ah（最小可达 {best_cap_spread:g} Ah）",
        )
    if not res_ok_any:
        return (
            "RESISTANCE_SPREAD_EXCEEDED",
            f"任意 {min_size}~{max_size} 节组合的内阻差都超过上限 "
            f"{max_res_delta:g} mΩ（最小可达 {best_res_spread:g} mΩ）",
        )
    return (
        "CONSTRAINTS_INCOMPATIBLE",
        f"容量差上限 {max_cap_delta:g} Ah 与内阻差上限 {max_res_delta:g} mΩ "
        "无法被同一组同时满足（各自可行，但不存在同时满足的组合）",
    )
