#!/usr/bin/env python3

import csv
import json
from pathlib import Path

ROOT = Path.home() / "CA_Project"
CHAR = ROOT / "results/official/characterization"
PERF = ROOT / "results/official/spec_summary.csv"
OUT = ROOT / "results/analysis/spec_structure_vs_performance.csv"

WORKLOADS = [
    "bwaves",
    "mcf",
    "lbm",
    "omnetpp",
    "xalancbmk",
    "fotonik3d",
]

CONFIGS = ["nopref", "ip_stride", "spp", "ampm"]

with PERF.open(newline="") as f:
    performance_rows = list(csv.DictReader(f))

performance = {}

for row in performance_rows:
    key = (row["workload"], row["config"])
    if key in performance:
        raise SystemExit(f"Duplicate experiment: {key}")
    performance[key] = row

expected = {(w, c) for w in WORKLOADS for c in CONFIGS}

if set(performance) != expected:
    raise SystemExit(
        f"Missing: {sorted(expected - set(performance))}\n"
        f"Unexpected: {sorted(set(performance) - expected)}"
    )

rows = []

for workload in WORKLOADS:
    p = CHAR / workload / "analysis_v3/summary.json"

    with p.open() as f:
        d = json.load(f)

    if d["accesses"] != 1_000_000:
        raise SystemExit(
            f"{workload}: unexpected sample count {d['accesses']}"
        )

    s = d["spatial"]
    a = d["ampm_structural"]

    perf = {
        c: performance[(workload, c)]
        for c in CONFIGS
    }

    row = {
        "workload": workload,
        "sample_accesses": d["accesses"],
        "sample_l2_hit_rate": d["hit_rate"],
        "sample_l2_miss_rate": d["miss_rate"],
        "unique_load_pcs": d["unique_load_pcs"],
        "supported_load_pcs": d["supported_load_pcs"],
        "unique_pages": s["pages"],
        "average_lines_per_page":
            s["average_unique_lines_per_page"],
        "footprint_density":
            s["average_footprint_density"],
        "recurring_footprint_fraction":
            s["recurring_footprint_page_fraction"],
        "best_page_jaccard":
            s["average_best_other_page_jaccard"],
        "jaccard_page_fraction_evaluated":
            s["similarity_page_fraction_evaluated"],
        "ampm_proxy_trigger_fraction":
            a["trigger_access_fraction"],
        "ampm_proxy_future_candidate_fraction":
            a["oracle_future_demand_fraction"],
        "ampm_proxy_potential_sampled_coverage":
            a["potential_sampled_miss_coverage"],
        "baseline_ipc":
            float(perf["nopref"]["ipc"]),
        "baseline_demand_misses":
            int(perf["nopref"]["l2_demand_misses"]),
        "ip_stride_speedup":
            float(perf["ip_stride"]["ipc_speedup"]),
        "spp_speedup":
            float(perf["spp"]["ipc_speedup"]),
        "ampm_speedup":
            float(perf["ampm"]["ipc_speedup"]),
        "ampm_measured_coverage":
            float(perf["ampm"]["coverage"]),
        "ampm_dram_traffic_ratio":
            float(perf["ampm"]["dram_traffic_ratio"]),
    }

    rows.append(row)

with OUT.open("w", newline="") as f:
    writer = csv.DictWriter(
        f, fieldnames=list(rows[0].keys())
    )
    writer.writeheader()
    writer.writerows(rows)

print("CROSS-WORKLOAD STRUCTURE VS PERFORMANCE")
print("=" * 106)

print(
    f"{'WORKLOAD':<13}"
    f"{'MISS%':>9}"
    f"{'FOOT%':>9}"
    f"{'TRIGGER%':>10}"
    f"{'STRIDE':>10}"
    f"{'SPP':>10}"
    f"{'AMPM':>10}"
    f"{'AMPM COV%':>12}"
    f"{'DRAM x':>10}"
)

for r in rows:
    print(
        f"{r['workload']:<13}"
        f"{100*r['sample_l2_miss_rate']:>9.2f}"
        f"{100*r['footprint_density']:>9.2f}"
        f"{100*r['ampm_proxy_trigger_fraction']:>10.2f}"
        f"{r['ip_stride_speedup']:>10.3f}"
        f"{r['spp_speedup']:>10.3f}"
        f"{r['ampm_speedup']:>10.3f}"
        f"{100*r['ampm_measured_coverage']:>12.2f}"
        f"{r['ampm_dram_traffic_ratio']:>10.3f}"
    )

print()
print(f"PASS: aggregated {len(rows)} workloads")
print(f"Output: {OUT}")
