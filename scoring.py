from __future__ import annotations
from typing import List, Tuple
import math

def score_folder(
    folder: str,
    distances: List[int],
    threshold: int,
    *,
    logger,
    debug: bool = False,
    quiet: bool = False,
) -> Tuple[float, float]:
    """Score a folder from distances over *threshold*.
    Returns (total_score, average). Logs detailed stats via provided logger.
    Behavior matches the original method except it's now a standalone function.
    """
    over_threshold = [x for x in distances if x > threshold]
    if not over_threshold:
        if not quiet:
            print(f"n/a: {folder} nothing over threshold {threshold}")
        return 0.0, -1.0

    scores = [100 * (x - threshold) / (128 - threshold) for x in over_threshold]
    total_score = sum(scores)
    scored_count = len(scores)
    average = total_score / scored_count if scored_count else 0.0
    if scored_count > 1:
        total_score *= math.log10(scored_count)

    # Over-threshold stats
    n_over = len(over_threshold)
    ot_sorted = sorted(over_threshold)
    ot_min, ot_max = ot_sorted[0], ot_sorted[-1]
    ot_mean = sum(over_threshold) / n_over
    ot_var = sum((v - ot_mean) ** 2 for v in over_threshold) / max(1, n_over - 1)
    ot_std = ot_var ** 0.5
    idx = lambda p: int(p * (n_over - 1))
    ot_p90, ot_p95, ot_p99 = ot_sorted[idx(0.90)], ot_sorted[idx(0.95)], ot_sorted[idx(0.99)]

    # Longest run of consecutive over-threshold values preserving order
    run = best_run = 0
    for v in distances:
        if v >= threshold:
            run += 1
            best_run = max(best_run, run)
        else:
            run = 0

    very_hi_cut = max(threshold + 15, 90)
    very_hi = sum(1 for v in over_threshold if v >= very_hi_cut)
    logger.info(
        ("Dist[%s] ≥%d: %d vals, mean %.1f±%.1f, span %d–%d, p90/95/99=%d/%d/%d, "
         "%d very-high (≥%d); longest ≥%d run=%d; total_score=%.1f avg=%.1f"),
        folder, threshold, n_over, ot_mean, ot_std, ot_min, ot_max,
        ot_p90, ot_p95, ot_p99, very_hi, very_hi_cut, threshold, best_run, total_score, average
    )

    sc_sorted = sorted(scores)
    sc_min, sc_max = sc_sorted[0], sc_sorted[-1]
    sc_mean = sum(scores) / n_over
    sc_var = sum((s - sc_mean) ** 2 for s in scores) / max(1, n_over - 1)
    sc_std = sc_var ** 0.5
    spct = lambda p: sc_sorted[int(p * (n_over - 1))]
    sc_p90, sc_p95, sc_p99 = spct(0.90), spct(0.95), spct(0.99)
    sc_very_cut = 95
    sc_very = sum(1 for s in scores if s >= sc_very_cut)
    logger.info(
        ("Score[%s] ≥%d: %d vals, mean %.1f±%.1f, span %.0f–%.0f, "
         "p90/95/99=%.0f/%.0f/%.0f, %d very-high (≥%d); total_score=%.1f avg=%.1f"),
        folder, threshold, n_over, sc_mean, sc_std, sc_min, sc_max,
        sc_p90, sc_p95, sc_p99, sc_very, sc_very_cut, total_score, average
    )

    if not quiet:
        print(average)
    return total_score, average
