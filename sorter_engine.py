import math
from typing import Dict, List, Tuple, Optional
import logging

def score_folder(
    folder: str,
    distances: List[int],
    threshold: int,
    logger: logging.Logger,
    debug: bool = False,
    quiet: bool = False,
) -> Tuple[float, float]:
    """Compute total_score and average for a folder given distances and threshold."""
    over_threshold = [x for x in distances if x > threshold]
    if not over_threshold:
        if not quiet:
            print("n/a: %s nothing over threshold %s" % (folder, threshold))
        return 0.0, -1.0

    scores = [100 * (x - threshold) / (128 - threshold) for x in over_threshold]
    total_score = sum(scores)
    scored_count = len(scores)
    average = total_score / scored_count if scored_count else 0.0
    if scored_count > 1:
        total_score *= math.log10(scored_count)

    # Summaries (over-threshold only)
    n_over = len(over_threshold)
    ot_sorted = sorted(over_threshold)
    ot_min, ot_max = ot_sorted[0], ot_sorted[-1]
    ot_mean = sum(over_threshold) / n_over
    ot_var = sum((v - ot_mean) ** 2 for v in over_threshold) / max(1, n_over - 1)
    ot_std = ot_var ** 0.5
    idx = lambda p: int(p * (n_over - 1))
    ot_p90, ot_p95, ot_p99 = ot_sorted[idx(0.90)], ot_sorted[idx(0.95)], ot_sorted[idx(0.99)]

    # Longest run of consecutive over-threshold values in the original order.
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


def decide_winner(
    dist_cache: Dict[str, List[int]],
    *,
    base_threshold: int,
    min_score: float,
    min_average: float,
    tie_ratio_gap: float,
    logger: logging.Logger,
    debug: bool = False,
    quiet: bool = False,
) -> Tuple[Optional[str], float]:
    """Run the threshold ladder and choose a winner. Returns (folder or None, score)."""
    T = base_threshold
    winning_folder: Optional[str] = None
    winning_score = 0.0

    while True:
        stats: Dict[str, Tuple[float, float]] = {}
        sum_av = 0.0
        best_pair: Tuple[float, float] | None = None  # (score, avg), best by (avg, then score)

        for f, d in dist_cache.items():
            sc, av = score_folder(f, d, T, logger, debug, quiet)
            stats[f] = (sc, av)
            sum_av += max(0.0, av)
            if (best_pair is None) or (av, sc) > (best_pair[1], best_pair[0]):
                best_pair = (sc, av)

        if sum_av <= 0.0:
            logger.info("T=%d | no over-threshold signal; skipping ladder", T)
            fails = "none"
            if best_pair is not None:
                bs, ba = best_pair
                parts = []
                if bs <= min_score:
                    parts.append(f"score {bs:.2f}/{min_score:.2f}")
                if ba <= min_average:
                    parts.append(f"avg {ba:.2f}/{min_average:.2f}")
                fails = "; ".join(parts) or "none"
            logger.info("RESOLVE @T=%d | no folder clears minimums; fails: %s; using new_folder", T, fails)
            break

        ranked = sorted(stats.items(), key=lambda it: (it[1][1], it[1][0]), reverse=True)
        lead_f, (lead_sc, lead_av) = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None

        r1 = (lead_av / sum_av) if sum_av > 0 else 0.0
        r2 = ((runner[1][1] / sum_av) if (sum_av > 0 and runner) else 0.0)
        ratio_gap = r1 - r2
        logger.info(
            "T=%d | leader=%s av=%.2f sc=%.2f | r1=%.3f r2=%.3f gap=%.3f",
            T, lead_f, lead_av, lead_sc, r1, r2, ratio_gap
        )

        if (not runner) or (ratio_gap >= tie_ratio_gap) or (T >= 125):
            if lead_sc > min_score and lead_av > min_average:
                winning_folder, winning_score = lead_f, lead_sc
                logger.info(
                    "RESOLVE @T=%d | winner=%s av=%.2f sc=%.2f (gap>=%.3f or no runner)",
                    T, winning_folder, lead_av, lead_sc, tie_ratio_gap,
                )
            else:
                parts = []
                if lead_sc <= min_score:
                    parts.append(f"score {lead_sc:.2f}/{min_score:.2f}")
                if lead_av <= min_average:
                    parts.append(f"avg {lead_av:.2f}/{min_average:.2f}")
                fails = "; ".join(parts) or "none"
                logger.info(
                    "RESOLVE @T=%d | no folder clears minimums; fails: %s; using new_folder",
                    T, fails
                )
            break
        else:
            T += 5
            logger.info("LADDER (ratio gap %.3f < %.3f) → raise T to %d", ratio_gap, tie_ratio_gap, T)

    return winning_folder, winning_score