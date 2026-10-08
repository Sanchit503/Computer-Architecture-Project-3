#!/usr/bin/env python3

import csv
import json
from pathlib import Path

ROOT = Path.home() / "CA_Project"

CHAR = ROOT / "results/official/characterization"
PERF = ROOT / "results/official/spec_summary.csv"
OUT = ROOT / "results/analysis"

OUT.mkdir(parents=True, exist_ok=True)

WORKLOADS = [
    "bwaves",
    "mcf",
    "lbm",
    "omnetpp",
    "xalancbmk",
    "fotonik3d",
]

CONFIGS = ["nopref", "ip_stride", "spp", "ampm"]


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows):
    if not rows:
        raise ValueError(f"No rows for {path}")

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0])
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"WROTE {path}: {len(rows)} rows")


perf_rows = read_csv(PERF)

perf = {}

for r in perf_rows:
    key = (r["workload"], r["config"])

    if key in perf:
        raise ValueError(f"Duplicate experiment: {key}")

    perf[key] = r

expected = {
    (w, c)
    for w in WORKLOADS
    for c in CONFIGS
}

assert set(perf) == expected, "Performance experiment set mismatch"

features = []
history_rows = []
pc_detail = []

for workload in WORKLOADS:

    base = CHAR / workload / "analysis_v3"

    with (base / "summary.json").open() as f:
        summary = json.load(f)

    pcs = read_csv(base / "pc_summary.csv")

    minimum = int(summary["pc_minimum_support"])

    supported = [
        p for p in pcs
        if int(p["accesses"]) >= minimum
    ]

    if len(supported) != summary["supported_load_pcs"]:
        raise ValueError(
            f"{workload}: supported PC count mismatch"
        )

    total_delta = sum(
        int(p["delta_count"])
        for p in supported
    )

    total_supported_accesses = sum(
        int(p["accesses"])
        for p in supported
    )

    total_supported_misses = sum(
        int(p["misses"])
        for p in supported
    )

    def delta_weighted(field):
        if not total_delta:
            return float("nan")

        return sum(
            int(p["delta_count"]) * float(p[field])
            for p in supported
        ) / total_delta

    feature = {
        "workload": workload,
        "sample_accesses": summary["accesses"],
        "unique_load_pcs": summary["unique_load_pcs"],
        "supported_load_pcs": len(supported),

        "supported_access_fraction":
            total_supported_accesses /
            summary["accesses"],

        "supported_miss_fraction":
            total_supported_misses /
            summary["misses"]
            if summary["misses"] else float("nan"),

        "pc_delta_weighted_dominant_fraction":
            delta_weighted("dominant_delta_fraction_all"),

        "pc_delta_weighted_stride_trigger":
            delta_weighted("ip_stride_trigger_rate"),

        "ip_stride_speedup":
            float(perf[(workload, "ip_stride")]["ipc_speedup"]),

        "spp_speedup":
            float(perf[(workload, "spp")]["ipc_speedup"]),

        "ampm_speedup":
            float(perf[(workload, "ampm")]["ipc_speedup"]),
    }

    for mode, key in [
        ("global", "global_delta_history"),
        ("page_local", "page_local_delta_history"),
    ]:

        histories = summary[key]

        if len(histories) != 4:
            raise ValueError(
                f"{workload}: expected four {mode} histories"
            )

        for h in histories:
            n = int(h["history_length"])

            if n not in (1, 2, 3, 4):
                raise ValueError(
                    f"Unexpected history length: {n}"
                )

            feature[f"{mode}_h{n}_supported"] = (
                h["supported_predictability"]
            )

            feature[f"{mode}_h{n}_repeat_coverage"] = (
                h["repeated_context_coverage"]
            )

            feature[f"{mode}_h{n}_repeat_accuracy"] = (
                h["repeated_context_top1_accuracy"]
            )

            history_rows.append({
                "workload": workload,
                "mode": mode,
                "history_length": n,
                "samples": h["total_prediction_samples"],
                "distinct_contexts": h["distinct_contexts"],
                "repeat_coverage":
                    h["repeated_context_coverage"],
                "repeat_accuracy":
                    h["repeated_context_top1_accuracy"],
                "supported_predictability":
                    h["supported_predictability"],
                "naive_top1_predictability":
                    h["naive_top1_predictability"],
            })

    features.append(feature)

    for p in sorted(
        supported,
        key=lambda x: int(x["misses"]),
        reverse=True
    ):
        pc_detail.append({
            "workload": workload,
            "ip": p["ip"],
            "accesses": int(p["accesses"]),
            "misses": int(p["misses"]),
            "miss_share": float(p["miss_share"]),
            "dominant_delta_all":
                p["dominant_delta_all"],
            "dominant_delta_fraction_all":
                float(p["dominant_delta_fraction_all"]),
            "stride_regularity":
                float(p["stride_regularity"]),
            "ip_stride_trigger_rate":
                float(p["ip_stride_trigger_rate"]),
        })


write_csv(
    OUT / "spec_pc_history_features.csv",
    features
)

write_csv(
    OUT / "spec_history_long.csv",
    history_rows
)

write_csv(
    OUT / "spec_supported_pcs.csv",
    pc_detail
)

assert len(features) == 6
assert len(history_rows) == 48

print()
print("PC-LEVEL / HISTORY-LEVEL SUMMARY")
print("=" * 92)

print(
    f"{'WORKLOAD':<13}"
    f"{'PC MISS%':>10}"
    f"{'DOM%':>9}"
    f"{'TRIG%':>9}"
    f"{'GLOBAL H2':>12}"
    f"{'PAGE H2':>11}"
    f"{'STRIDE x':>11}"
)

for r in features:
    print(
        f"{r['workload']:<13}"
        f"{100*r['supported_miss_fraction']:>10.2f}"
        f"{100*r['pc_delta_weighted_dominant_fraction']:>9.2f}"
        f"{100*r['pc_delta_weighted_stride_trigger']:>9.2f}"
        f"{100*r['global_h2_supported']:>12.2f}"
        f"{100*r['page_local_h2_supported']:>11.2f}"
        f"{r['ip_stride_speedup']:>11.3f}"
    )

print()
print("PASS: 6 workloads, 48 history rows")
print(f"PASS: {len(pc_detail)} supported-PC rows")

