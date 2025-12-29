"""
Helpers for per-position dollar-denominated brackets.

Topstep brackets are configured in dollars per position. When size increases,
the stop/target distance in points shrinks because the dollar risk is spread
across more contracts. Use these helpers to compute deterministic distances and
apply simple sizing gates (e.g., minimum stop distance) before dispatching
orders.
"""
from typing import Dict, Iterable, Optional, Tuple


def compute_bracket_distances(
    sl_usd: float,
    tp_usd: float,
    size: int,
    *,
    point_value: float = 5.0,
    tick_size: float = 0.25,
) -> Dict:
    """
    Calculate per-position bracket distances for futures with server-side OCO.

    points = usd / (point_value_per_contract * size)
    ticks = round(points / tick_size)  (floor to at least 1 tick)
    points are normalized to ticks * tick_size to reflect tradable distances.
    """
    if size <= 0:
        raise ValueError("size must be positive")

    sl_points_raw = sl_usd / (point_value * size)
    tp_points_raw = tp_usd / (point_value * size)

    sl_ticks = max(1, round(sl_points_raw / tick_size))
    tp_ticks = max(1, round(tp_points_raw / tick_size))

    sl_points = sl_ticks * tick_size
    tp_points = tp_ticks * tick_size

    return {
        "sl_usd": sl_usd,
        "tp_usd": tp_usd,
        "size": size,
        "sl_points_raw": sl_points_raw,
        "tp_points_raw": tp_points_raw,
        "sl_ticks": sl_ticks,
        "tp_ticks": tp_ticks,
        "sl_points": sl_points,
        "tp_points": tp_points,
        "point_value": point_value,
        "tick_size": tick_size,
    }


def compute_bracket_table(
    sl_usd: float,
    tp_usd: float,
    sizes: Iterable[int] = (1, 2, 3),
    point_value: float = 5.0,
    tick_size: float = 0.25,
) -> Dict[str, Dict]:
    """Compute bracket distances for a set of sizes."""
    table: Dict[str, Dict] = {}
    for size in sizes:
        key = f"size_{size}"
        table[key] = compute_bracket_distances(
            sl_usd,
            tp_usd,
            int(size),
            point_value=point_value,
            tick_size=tick_size,
        )
    return table


def clamp_size_for_min_stop(
    size: int,
    sl_usd: float,
    tp_usd: float,
    *,
    point_value: float = 5.0,
    tick_size: float = 0.25,
    min_sl_points: float = None,
    min_sl_ticks: int = None,
) -> Tuple[int, Optional[Dict]]:
    """
    Reduce size until the stop distance meets minimum requirements.

    Returns (new_size, last_distances). If new_size == 0, callers should block
    the trade (convert to HOLD).
    """
    if size is None:
        return 0, None

    try:
        size = int(size)
    except (TypeError, ValueError):
        return 0, None

    if size <= 0:
        return 0, None

    threshold_points = None
    if min_sl_ticks is not None:
        try:
            threshold_points = float(min_sl_ticks) * tick_size
        except (TypeError, ValueError):
            threshold_points = None
    elif min_sl_points is not None:
        try:
            threshold_points = float(min_sl_points)
        except (TypeError, ValueError):
            threshold_points = None

    distances = None
    while size > 0:
        distances = compute_bracket_distances(
            sl_usd,
            tp_usd,
            size,
            point_value=point_value,
            tick_size=tick_size,
        )
        if threshold_points is None or distances["sl_points"] >= threshold_points:
            break
        size -= 1

    return size, distances
