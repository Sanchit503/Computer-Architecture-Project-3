#!/usr/bin/env python3

import argparse
import csv
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path


def safe_div(a, b):
    return a / b if b else 0.0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Characterize L2 demand-access predictability."
    )
    parser.add_argument(
        "input_csv",
        type=Path,
        help="CSV produced by CHAMPSIM_L2_DUMP"
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        required=True,
        help="Directory for analysis outputs"
    )
    parser.add_argument(
        "--max-history",
        type=int,
        default=4,
        help="Maximum ordered delta-history length"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top PCs/pages to print"
    )
    parser.add_argument(
        "--min-pc-accesses",
        type=int,
        default=1000,
        help="Minimum sampled accesses required for a PC to appear in the main PC table"
    )
    return parser.parse_args()


def load_trace(path):
    rows = []

    with path.open(newline="") as f:
        reader = csv.DictReader(f)

        required = {
            "seq",
            "ip",
            "address",
            "line_addr",
            "page",
            "line_offset",
            "hit",
            "instr_id",
            "cycle",
        }

        if reader.fieldnames is None:
            raise RuntimeError("CSV has no header")

        missing = required - set(reader.fieldnames)
        if missing:
            raise RuntimeError(
                f"CSV missing required fields: {sorted(missing)}"
            )

        for r in reader:
            rows.append(
                {
                    "seq": int(r["seq"]),
                    "ip": r["ip"],
                    "address": int(r["address"], 16),
                    "line_addr": int(r["line_addr"]),
                    "page": int(r["page"]),
                    "line_offset": int(r["line_offset"]),
                    "hit": int(r["hit"]),
                    "instr_id": int(r["instr_id"]),
                    "cycle": int(r["cycle"]),
                }
            )

    rows.sort(key=lambda x: x["seq"])
    return rows


def analyze_pc(rows):
    by_pc = defaultdict(list)

    for r in rows:
        by_pc[r["ip"]].append(r)

    total_misses = sum(1 for r in rows if r["hit"] == 0)

    result = []

    for ip, accesses in by_pc.items():
        lines = [x["line_addr"] for x in accesses]

        deltas = [
            lines[i] - lines[i - 1]
            for i in range(1, len(lines))
        ]

        # ----------------------------------------------------
        # Assignment-literal dominant-delta metric.
        #
        # This includes zero deltas because the handout asks
        # for the most frequent address delta and the fraction
        # of successive accesses represented by that delta.
        # ----------------------------------------------------
        all_delta_counts = Counter(deltas)

        if all_delta_counts:
            dominant_delta_all, dominant_delta_all_count = (
                all_delta_counts.most_common(1)[0]
            )
        else:
            dominant_delta_all = 0
            dominant_delta_all_count = 0

        dominant_delta_fraction_all = safe_div(
            dominant_delta_all_count,
            len(deltas)
        )

        # ----------------------------------------------------
        # Supplemental IP-stride-oriented metrics.
        #
        # The real IP-stride predictor requires a non-zero
        # repeated stride, so preserve the original analysis.
        # ----------------------------------------------------
        nonzero = [d for d in deltas if d != 0]
        nonzero_counts = Counter(nonzero)

        if nonzero_counts:
            dominant_stride, dominant_stride_count = (
                nonzero_counts.most_common(1)[0]
            )
        else:
            dominant_stride = 0
            dominant_stride_count = 0

        stride_regularity = safe_div(
            dominant_stride_count,
            len(nonzero)
        )

        dominant_nonzero_stride_fraction_all = safe_div(
            dominant_stride_count,
            len(deltas)
        )

        # Closely reflects the condition used by ip_stride:
        # current stride != 0 and current stride == previous stride.
        repeated_stride_matches = 0
        repeated_stride_trials = 0

        for i in range(1, len(deltas)):
            repeated_stride_trials += 1

            if deltas[i] != 0 and deltas[i] == deltas[i - 1]:
                repeated_stride_matches += 1

        accesses_count = len(accesses)
        misses = sum(1 for x in accesses if x["hit"] == 0)
        hits = accesses_count - misses

        result.append(
            {
                "ip": ip,
                "accesses": accesses_count,
                "hits": hits,
                "misses": misses,
                "miss_rate": safe_div(misses, accesses_count),
                "miss_share": safe_div(misses, total_misses),

                "delta_count": len(deltas),
                "zero_delta_count":
                    len(deltas) - len(nonzero),
                "nonzero_delta_count": len(nonzero),

                # Assignment-required quantities.
                "dominant_delta_all":
                    dominant_delta_all,
                "dominant_delta_all_count":
                    dominant_delta_all_count,
                "dominant_delta_fraction_all":
                    dominant_delta_fraction_all,

                # Supplemental IP-stride quantities.
                "dominant_stride":
                    dominant_stride,
                "dominant_stride_count":
                    dominant_stride_count,
                "stride_regularity":
                    stride_regularity,
                "dominant_nonzero_stride_fraction_all":
                    dominant_nonzero_stride_fraction_all,

                "ip_stride_trigger_rate": safe_div(
                    repeated_stride_matches,
                    repeated_stride_trials
                ),
            }
        )

    result.sort(
        key=lambda x: (x["misses"], x["accesses"]),
        reverse=True
    )

    return result


def build_page_delta_streams(rows):
    """
    Build page-local delta streams.

    SPP tracks the previous offset within each page and forms deltas from
    cache-line offsets. Zero deltas are excluded from signature evolution.
    """

    last_offset = {}
    page_deltas = defaultdict(list)

    for r in rows:
        page = r["page"]
        current = r["line_offset"]

        if page in last_offset:
            delta = current - last_offset[page]

            if delta != 0:
                page_deltas[page].append(delta)

        last_offset[page] = current

    return page_deltas


def analyze_delta_history(rows, max_history):
    page_deltas = build_page_delta_streams(rows)

    output = []

    for history_len in range(1, max_history + 1):
        transitions = defaultdict(Counter)
        total_samples = 0

        for deltas in page_deltas.values():
            if len(deltas) <= history_len:
                continue

            for i in range(history_len, len(deltas)):
                history = tuple(
                    deltas[i - history_len:i]
                )

                next_delta = deltas[i]

                transitions[history][next_delta] += 1
                total_samples += 1

        all_correct = 0

        repeated_samples = 0
        repeated_correct = 0
        repeated_contexts = 0

        for history, next_counts in transitions.items():
            occurrences = sum(next_counts.values())
            best = max(next_counts.values())

            all_correct += best

            # Contexts seen only once trivially appear 100% predictable.
            # Keep them out of the stronger "repeated" metric.
            if occurrences >= 2:
                repeated_contexts += 1
                repeated_samples += occurrences
                repeated_correct += best

        output.append(
            {
                "history_length": history_len,
                "distinct_contexts": len(transitions),
                "total_prediction_samples": total_samples,

                "naive_top1_predictability":
                    safe_div(all_correct, total_samples),

                "repeated_contexts": repeated_contexts,
                "repeated_context_samples": repeated_samples,

                "repeated_context_coverage":
                    safe_div(repeated_samples, total_samples),

                "repeated_context_top1_accuracy":
                    safe_div(repeated_correct, repeated_samples),

                "supported_predictability":
                    safe_div(repeated_correct, total_samples),
            }
        )

    return output


def analyze_global_delta_history(rows, max_history, top_patterns=20):
    """
    Analyze ordered deltas in the chronological sampled L2 demand stream.

    Unlike analyze_delta_history(), this function does not separate accesses
    by page. It follows the complete line-address sequence:

        A0, A1, A2, ...

    and forms:

        delta_i = A_i - A_(i-1)

    Zero deltas are retained because this is a characterization of the
    observed L2 address stream, not a simulation of SPP's signature update.
    """

    lines = [r["line_addr"] for r in rows]

    deltas = [
        lines[i] - lines[i - 1]
        for i in range(1, len(lines))
    ]

    output = []
    pattern_rows = []

    for history_len in range(1, max_history + 1):
        transitions = defaultdict(Counter)

        total_samples = 0

        for i in range(history_len, len(deltas)):
            history = tuple(
                deltas[i - history_len:i]
            )

            next_delta = deltas[i]

            transitions[history][next_delta] += 1
            total_samples += 1

        all_correct = 0

        repeated_samples = 0
        repeated_correct = 0
        repeated_contexts = 0

        context_rows = []

        for history, next_counts in transitions.items():
            occurrences = sum(next_counts.values())

            top_next_delta, best = next_counts.most_common(1)[0]

            all_correct += best

            if occurrences >= 2:
                repeated_contexts += 1
                repeated_samples += occurrences
                repeated_correct += best

                context_rows.append(
                    {
                        "history_length": history_len,
                        "history": " ".join(
                            str(x) for x in history
                        ),
                        "occurrences": occurrences,
                        "top_next_delta": top_next_delta,
                        "top_next_count": best,
                        "top1_accuracy":
                            safe_div(best, occurrences),
                    }
                )

        output.append(
            {
                "history_length": history_len,
                "distinct_contexts": len(transitions),
                "total_prediction_samples": total_samples,

                "naive_top1_predictability":
                    safe_div(all_correct, total_samples),

                "repeated_contexts":
                    repeated_contexts,

                "repeated_context_samples":
                    repeated_samples,

                "repeated_context_coverage":
                    safe_div(
                        repeated_samples,
                        total_samples
                    ),

                "repeated_context_top1_accuracy":
                    safe_div(
                        repeated_correct,
                        repeated_samples
                    ),

                "supported_predictability":
                    safe_div(
                        repeated_correct,
                        total_samples
                    ),
            }
        )

        context_rows.sort(
            key=lambda x: (
                x["occurrences"],
                x["top_next_count"]
            ),
            reverse=True
        )

        pattern_rows.extend(
            context_rows[:top_patterns]
        )

    return output, pattern_rows


def analyze_pages(rows):
    page_info = defaultdict(
        lambda: {
            "accesses": 0,
            "misses": 0,
            "offsets": set(),
        }
    )

    for r in rows:
        p = page_info[r["page"]]

        p["accesses"] += 1
        p["misses"] += (r["hit"] == 0)
        p["offsets"].add(r["line_offset"])

    footprint_counter = Counter()
    mask_counter = Counter()

    page_footprint = {}
    page_mask = {}

    for page, info in page_info.items():
        footprint = tuple(sorted(info["offsets"]))

        mask = 0
        for offset in footprint:
            mask |= (1 << offset)

        page_footprint[page] = footprint
        page_mask[page] = mask

        footprint_counter[footprint] += 1
        mask_counter[mask] += 1

    # --------------------------------------------------------
    # Exact footprint recurrence.
    # --------------------------------------------------------
    recurring_patterns = {
        fp: count
        for fp, count in footprint_counter.items()
        if count >= 2
    }

    pages_in_recurring_patterns = sum(
        recurring_patterns.values()
    )

    # --------------------------------------------------------
    # Similar-footprint characterization.
    #
    # Jaccard(A,B) = |A intersection B| / |A union B|
    #
    # Compare distinct footprint bitmaps rather than every page
    # pair. Repeated exact patterns trivially have best Jaccard
    # similarity 1.0.
    # --------------------------------------------------------
    MAX_EXACT_SIMILARITY_PATTERNS = 5000

    pattern_items = sorted(
        mask_counter.items(),
        key=lambda x: (-x[1], x[0])
    )

    exact_similarity_all_patterns = (
        len(pattern_items)
        <= MAX_EXACT_SIMILARITY_PATTERNS
    )

    reference_masks = [
        mask
        for mask, _ in
        pattern_items[:MAX_EXACT_SIMILARITY_PATTERNS]
    ]

    reference_set = set(reference_masks)

    best_jaccard_by_mask = {}

    for mask, count in pattern_items:
        # Two or more pages share this exact footprint.
        if count >= 2:
            best_jaccard_by_mask[mask] = 1.0
            continue

        # If there are too many distinct patterns, bound the
        # expensive pairwise comparison. Unexamined unique
        # footprints remain None and are reported explicitly.
        if mask not in reference_set:
            best_jaccard_by_mask[mask] = None
            continue

        best = None

        for other in reference_masks:
            if other == mask:
                continue

            intersection = (mask & other).bit_count()
            union = (mask | other).bit_count()

            similarity = safe_div(
                intersection,
                union
            )

            if best is None or similarity > best:
                best = similarity

        best_jaccard_by_mask[mask] = best

    result = []

    evaluated_page_similarities = []

    for page, info in page_info.items():
        footprint = page_footprint[page]
        mask = page_mask[page]

        best_similarity = best_jaccard_by_mask[mask]

        if best_similarity is not None:
            evaluated_page_similarities.append(
                best_similarity
            )

        result.append(
            {
                "page": page,
                "accesses": info["accesses"],
                "misses": info["misses"],
                "unique_lines": len(info["offsets"]),
                "footprint_density":
                    len(info["offsets"]) / 64.0,
                "best_other_page_jaccard":
                    best_similarity,
                "footprint": " ".join(
                    str(x) for x in footprint
                ),
            }
        )

    result.sort(
        key=lambda x: (
            x["accesses"],
            x["misses"]
        ),
        reverse=True
    )

    similarity_pages_evaluated = len(
        evaluated_page_similarities
    )

    spatial_summary = {
        "pages":
            len(page_info),

        "average_unique_lines_per_page":
            safe_div(
                sum(
                    len(x["offsets"])
                    for x in page_info.values()
                ),
                len(page_info)
            ),

        "average_footprint_density":
            safe_div(
                sum(
                    len(x["offsets"]) / 64.0
                    for x in page_info.values()
                ),
                len(page_info)
            ),

        "distinct_exact_footprints":
            len(footprint_counter),

        "recurring_exact_footprints":
            len(recurring_patterns),

        "pages_in_recurring_exact_footprints":
            pages_in_recurring_patterns,

        "recurring_footprint_page_fraction":
            safe_div(
                pages_in_recurring_patterns,
                len(page_info)
            ),

        # Similar-footprint metrics.
        "footprint_similarity_metric":
            "best-other-page Jaccard similarity",

        "footprint_similarity_exact_all_patterns":
            exact_similarity_all_patterns,

        "similarity_pages_evaluated":
            similarity_pages_evaluated,

        "similarity_page_fraction_evaluated":
            safe_div(
                similarity_pages_evaluated,
                len(page_info)
            ),

        "average_best_other_page_jaccard":
            safe_div(
                sum(evaluated_page_similarities),
                similarity_pages_evaluated
            ),

        "pages_best_jaccard_ge_0_50":
            sum(
                x >= 0.50
                for x in evaluated_page_similarities
            ),

        "fraction_best_jaccard_ge_0_50":
            safe_div(
                sum(
                    x >= 0.50
                    for x in evaluated_page_similarities
                ),
                similarity_pages_evaluated
            ),

        "pages_best_jaccard_ge_0_75":
            sum(
                x >= 0.75
                for x in evaluated_page_similarities
            ),

        "fraction_best_jaccard_ge_0_75":
            safe_div(
                sum(
                    x >= 0.75
                    for x in evaluated_page_similarities
                ),
                similarity_pages_evaluated
            ),

        "pages_best_jaccard_ge_0_90":
            sum(
                x >= 0.90
                for x in evaluated_page_similarities
            ),

        "fraction_best_jaccard_ge_0_90":
            safe_div(
                sum(
                    x >= 0.90
                    for x in evaluated_page_similarities
                ),
                similarity_pages_evaluated
            ),
    }

    return result, spatial_summary


def analyze_ampm_page_local(rows):
    """
    Dynamic page-local structural proxy for va_ampm_lite.

    This intentionally does NOT claim to reproduce the real AMPM prefetch
    count. The hardware implementation can search across page boundaries,
    maintains a finite 128-region LRU table, and only increments its degree
    after prefetch_line() succeeds. Those effects depend on the prefetcher's
    own runtime behavior and cannot be reconstructed exactly from a baseline
    no-prefetch trace.

    This metric instead asks:

        When examining the chronological demand stream, how often does the
        AMPM equal-spacing rule expose a same-page candidate?

    We mirror several important implementation details:
      * first touch of a page allocates state but is not marked
      * current demand is marked before searching
      * positive direction is searched before negative direction
      * candidate requires two equally-spaced prior accesses
      * already-accessed or already-proposed lines are suppressed
      * degree is at most 2 per direction
      * MAX_DISTANCE is 256, although same-page bounds reduce the
        effective search distance considerably

    Every structural candidate is treated as if admission succeeded.
    """

    BLOCKS_PER_PAGE = 64
    MAX_DISTANCE = 256
    DEGREE_PER_DIRECTION = 2

    # Future positions allow us to ask whether a structural candidate
    # is actually demanded later in the sampled baseline stream.
    positions_by_line = defaultdict(list)

    for idx, r in enumerate(rows):
        positions_by_line[r["line_addr"]].append(idx)

    # Persistent per-page state for this structural characterization.
    # We intentionally do not claim this models the real 128-entry LRU.
    initialized_pages = set()
    access_map = defaultdict(set)
    prefetch_map = defaultdict(set)

    candidate_count = 0
    trigger_accesses = 0
    oracle_future_demand_candidates = 0
    targeted_future_miss_indices = set()

    future_lead_sum = 0
    future_lead_samples = 0

    accesses_after_first_touch = 0

    for idx, r in enumerate(rows):
        page = r["page"]
        current = r["line_offset"]

        # Mirrors va_ampm_lite: first touch allocates a region and returns
        # before marking the accessed line.
        if page not in initialized_pages:
            initialized_pages.add(page)
            continue

        accesses_after_first_touch += 1

        # va_ampm_lite marks the current demand before searching.
        access_map[page].add(current)

        generated_this_access = 0

        # Positive direction first, then negative.
        for direction in (1, -1):
            generated_this_direction = 0

            for distance in range(1, MAX_DISTANCE + 1):
                target = current + direction * distance
                prior_1 = current - direction * distance
                prior_2 = current - direction * 2 * distance

                # This proxy considers only the subset where all relevant
                # lines remain in the same 4-KiB page.
                if not (
                    0 <= target < BLOCKS_PER_PAGE
                    and 0 <= prior_1 < BLOCKS_PER_PAGE
                    and 0 <= prior_2 < BLOCKS_PER_PAGE
                ):
                    break

                if (
                    prior_1 in access_map[page]
                    and prior_2 in access_map[page]
                    and target not in access_map[page]
                    and target not in prefetch_map[page]
                ):
                    candidate_count += 1
                    generated_this_access += 1
                    generated_this_direction += 1

                    # Assume structural admission succeeds so duplicate
                    # proposals are suppressed just like prefetch_map.
                    prefetch_map[page].add(target)

                    target_line = page * BLOCKS_PER_PAGE + target
                    future_positions = positions_by_line.get(target_line, [])

                    pos = bisect_right(future_positions, idx)

                    if pos < len(future_positions):
                        next_idx = future_positions[pos]

                        oracle_future_demand_candidates += 1
                        future_lead_sum += next_idx - idx
                        future_lead_samples += 1

                        # If the next baseline demand to this candidate was
                        # an L2 miss, this target represents a potential
                        # miss-covering opportunity.
                        if rows[next_idx]["hit"] == 0:
                            targeted_future_miss_indices.add(next_idx)

                    if generated_this_direction >= DEGREE_PER_DIRECTION:
                        break

        if generated_this_access:
            trigger_accesses += 1

    total_misses = sum(1 for r in rows if r["hit"] == 0)

    return {
        "model":
            "page-local structural AMPM proxy; not exact hardware simulation",

        "max_distance":
            MAX_DISTANCE,

        "degree_per_direction":
            DEGREE_PER_DIRECTION,

        "pages_initialized":
            len(initialized_pages),

        "accesses_after_first_page_touch":
            accesses_after_first_touch,

        "trigger_accesses":
            trigger_accesses,

        "trigger_access_fraction":
            safe_div(trigger_accesses, accesses_after_first_touch),

        "structural_candidates":
            candidate_count,

        "candidates_per_eligible_access":
            safe_div(candidate_count, accesses_after_first_touch),

        "oracle_future_demand_candidates":
            oracle_future_demand_candidates,

        "oracle_future_demand_fraction":
            safe_div(oracle_future_demand_candidates, candidate_count),

        "unique_future_baseline_misses_targeted":
            len(targeted_future_miss_indices),

        "potential_sampled_miss_coverage":
            safe_div(len(targeted_future_miss_indices), total_misses),

        "mean_future_lead_accesses":
            safe_div(future_lead_sum, future_lead_samples),

        "limitations": [
            "same-page candidate subset only",
            "does not model 128-region LRU replacement",
            "assumes every structural candidate is admitted",
            "does not model MSHR occupancy or prefetch queue failure",
            "oracle future-demand fields use future knowledge only for characterization"
        ]
    }


def write_csv(path, rows):
    if not rows:
        return

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys())
        )

        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()

    args.outdir.mkdir(
        parents=True,
        exist_ok=True
    )

    rows = load_trace(args.input_csv)

    if not rows:
        raise RuntimeError("Input trace contains no accesses")

    accesses = len(rows)
    hits = sum(r["hit"] for r in rows)
    misses = accesses - hits

    pc_stats = analyze_pc(rows)

    # Page-local history: supplemental SPP-aligned view.
    page_local_delta_history = analyze_delta_history(
        rows,
        args.max_history
    )

    # Chronological global L2 stream: assignment-literal view.
    global_delta_history, global_delta_patterns = (
        analyze_global_delta_history(
            rows,
            args.max_history
        )
    )

    # Backward-compatible local variable for the existing
    # page-local print loop below.
    delta_history = page_local_delta_history

    page_stats, spatial_summary = analyze_pages(rows)

    ampm_structural = analyze_ampm_page_local(rows)

    supported_pc_stats = [
        x for x in pc_stats
        if x["accesses"] >= args.min_pc_accesses
    ]

    summary = {
        "input_file": str(args.input_csv),
        "accesses": accesses,
        "hits": hits,
        "misses": misses,
        "hit_rate": safe_div(hits, accesses),
        "miss_rate": safe_div(misses, accesses),
        "unique_load_pcs": len(pc_stats),
        "pc_minimum_support": args.min_pc_accesses,
        "supported_load_pcs": len(supported_pc_stats),
        "spatial": spatial_summary,
        "ampm_structural": ampm_structural,
        "global_delta_history":
            global_delta_history,
        "page_local_delta_history":
            page_local_delta_history,
    }

    write_csv(
        args.outdir / "pc_summary.csv",
        pc_stats
    )

    write_csv(
        args.outdir / "global_delta_history.csv",
        global_delta_history
    )

    write_csv(
        args.outdir / "global_delta_patterns.csv",
        global_delta_patterns
    )

    write_csv(
        args.outdir / "page_local_delta_history.csv",
        delta_history
    )

    write_csv(
        args.outdir / "page_summary.csv",
        page_stats
    )

    with (args.outdir / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print()
    print("========================================")
    print("L2 MEMORY CHARACTERIZATION")
    print("========================================")
    print(f"Input accesses : {accesses}")
    print(f"L2 hits        : {hits}")
    print(f"L2 misses      : {misses}")
    print(f"L2 hit rate    : {safe_div(hits, accesses):.4f}")
    print(f"Unique PCs     : {len(pc_stats)}")
    print(f"Unique pages   : {spatial_summary['pages']}")

    print()
    print("Top PCs by L2 misses")
    print("----------------------------------------")

    print(
        f"{'PC':>12} "
        f"{'ACC':>8} "
        f"{'MISS':>8} "
        f"{'MISS%':>8} "
        f"{'DOM_DELTA':>9} "
        f"{'DOM_FRAC':>8} "
        f"{'TRIGGER':>8}"
    )

    print(
        f"(showing PCs with >= {args.min_pc_accesses} sampled accesses)"
    )

    for x in supported_pc_stats[:args.top]:
        print(
            f"{x['ip']:>12} "
            f"{x['accesses']:8d} "
            f"{x['misses']:8d} "
            f"{100*x['miss_share']:7.2f}% "
            f"{x['dominant_delta_all']:9d} "
            f"{x['dominant_delta_fraction_all']:8.3f} "
            f"{x['ip_stride_trigger_rate']:8.3f}"
        )

    print()
    print("Ordered GLOBAL L2 delta-history predictability")
    print("----------------------------------------")
    print("  HIST    SAMPLES   REPEAT_COV   REPEAT_ACC    SUPPORTED")

    for x in global_delta_history:
        print(
            f"{x['history_length']:6d} "
            f"{x['total_prediction_samples']:10d} "
            f"{x['repeated_context_coverage']:12.3f} "
            f"{x['repeated_context_top1_accuracy']:12.3f} "
            f"{x['supported_predictability']:12.3f}"
        )

    print()
    print("Ordered PAGE-LOCAL delta-history predictability")
    print("----------------------------------------")

    print(
        f"{'HIST':>6} "
        f"{'SAMPLES':>10} "
        f"{'REPEAT_COV':>12} "
        f"{'REPEAT_ACC':>12} "
        f"{'SUPPORTED':>12}"
    )

    for x in delta_history:
        print(
            f"{x['history_length']:6d} "
            f"{x['total_prediction_samples']:10d} "
            f"{x['repeated_context_coverage']:12.3f} "
            f"{x['repeated_context_top1_accuracy']:12.3f} "
            f"{x['supported_predictability']:12.3f}"
        )

    print()
    print("Spatial summary")
    print("----------------------------------------")

    for key, value in spatial_summary.items():
        if isinstance(value, float):
            print(f"{key:38s}: {value:.4f}")
        else:
            print(f"{key:38s}: {value}")

    print()
    print("AMPM-style structural opportunity proxy")
    print("----------------------------------------")

    for key, value in ampm_structural.items():
        if key == "limitations":
            continue

        if isinstance(value, float):
            print(f"{key:38s}: {value:.4f}")
        else:
            print(f"{key:38s}: {value}")

    print()
    print("Wrote:")
    print(args.outdir / "summary.json")
    print(args.outdir / "pc_summary.csv")
    print(args.outdir / "global_delta_history.csv")
    print(args.outdir / "global_delta_patterns.csv")
    print(args.outdir / "page_local_delta_history.csv")
    print(args.outdir / "page_summary.csv")


if __name__ == "__main__":
    main()
