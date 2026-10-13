#!/usr/bin/env python3
"""Export reproducible midsem summary tables."""

import csv
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"

SUITES = {
    "spec": [
        "bwaves", "mcf", "lbm",
        "omnetpp", "xalancbmk", "fotonik3d"
    ],
    "course": [
        "bfscc", "pagerankdelta", "triangle", "llama2"
    ],
}

CONFIGS = ["nopref", "ip_stride", "spp", "ampm"]

METRICS = [
    "instructions", "cycles", "ipc",
    "l2_demand_misses", "l2_demand_mpki",
    "pf_requested", "pf_issued", "pf_filled",
    "pf_useful", "pf_useless", "pf_late",
    "dram_total_traffic",
]


def load(path):
    assert path.is_file(), f"Missing: {path}"

    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1, f"Invalid row count: {path}"
    return rows[0]


def source_path(suite, workload, config, fixed=False):
    if fixed:
        d = (
            ROOT / "results/validation/spp_bounds_fixed"
            / workload
        )

        assert not (d / "FAILED").exists()

        if workload != "bwaves":
            assert (d / "DONE").is_file()

        log = d / "run.log"

        assert log.is_file()
        assert "Simulation finished CPU 0" in (
            log.read_text(errors="replace")
        )
    else:
        d = (
            ROOT / "results/official"
            / suite / workload / config
        )

        assert (d / "DONE").is_file()
        assert not (d / "FAILED").exists()

    return d / "result.csv"


def fmt(x):
    return str(x.quantize(Decimal("0.0000000001")))


raw = {}
comparisons = []

for suite, workloads in SUITES.items():
    for workload in workloads:

        for config in CONFIGS:
            path = source_path(
                suite, workload, config,
                fixed=(config == "spp")
            )

            row = load(path)

            assert all(k in row for k in METRICS)
            assert int(row["instructions"]) >= 100_000_000
            assert Decimal(row["ipc"]) > 0

            raw[(suite, workload, config)] = (row, path)

        original = load(
            source_path(suite, workload, "spp")
        )

        corrected = raw[
            (suite, workload, "spp")
        ][0]

        shared = (
            set(original) & set(corrected)
        ) - {"json_file"}

        changed = sum(
            original[k] != corrected[k]
            for k in shared
        )

        old_ipc = Decimal(original["ipc"])
        new_ipc = Decimal(corrected["ipc"])

        comparisons.append({
            "suite": suite,
            "workload": workload,
            "original_spp_ipc": original["ipc"],
            "corrected_spp_ipc": corrected["ipc"],
            "ipc_change_percent": fmt(
                (new_ipc / old_ipc - 1) * 100
            ),
            "changed_nonpath_fields": changed,
        })


columns = [
    "suite", "workload", "config",
    "source_variant", "source_csv",
    *METRICS,
    "ipc_speedup_vs_nopref",
    "l2_demand_miss_reduction_vs_nopref",
    "dram_traffic_ratio_vs_nopref",
]

summary = []

for suite, workloads in SUITES.items():
    for workload in workloads:

        baseline = raw[
            (suite, workload, "nopref")
        ][0]

        b_ipc = Decimal(baseline["ipc"])
        b_misses = Decimal(
            baseline["l2_demand_misses"]
        )
        b_dram = Decimal(
            baseline["dram_total_traffic"]
        )

        assert b_ipc > 0
        assert b_misses > 0
        assert b_dram > 0

        for config in CONFIGS:
            values, source = raw[
                (suite, workload, config)
            ]

            summary.append({
                "suite": suite,
                "workload": workload,
                "config": config,
                "source_variant":
                    "bounds_fixed" if config == "spp"
                    else "original",
                "source_csv": str(
                    source.relative_to(ROOT)
                ),
                **{
                    k: values[k]
                    for k in METRICS
                },
                "ipc_speedup_vs_nopref": fmt(
                    Decimal(values["ipc"]) / b_ipc
                ),
                "l2_demand_miss_reduction_vs_nopref":
                    fmt(
                        1 - Decimal(
                            values["l2_demand_misses"]
                        ) / b_misses
                    ),
                "dram_traffic_ratio_vs_nopref": fmt(
                    Decimal(
                        values["dram_total_traffic"]
                    ) / b_dram
                ),
            })


assert len(summary) == 40
assert len(comparisons) == 10

OUT.mkdir(parents=True, exist_ok=True)

exports = [
    (
        OUT / "midsem_40runs_corrected_spp.csv",
        columns,
        summary,
    ),
    (
        OUT / "spp_original_vs_corrected.csv",
        list(comparisons[0]),
        comparisons,
    ),
]

for path, fields, rows in exports:
    assert not path.exists(), (
        f"STOP: Existing export: {path}"
    )

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"WROTE: {path.relative_to(ROOT)} "
        f"({len(rows)} rows)"
    )

print("PASS: GitHub results exported")
