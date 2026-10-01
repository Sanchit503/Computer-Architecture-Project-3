#!/usr/bin/env python3

import argparse
import csv
import json
import math
from pathlib import Path


def safe_div(num, den):
    return num / den if den else 0.0


def scalar_or_sum(value):
    """
    ChampSim single-core cache counters are often represented as [value].
    This also works if a future config has multiple CPU-origin entries.
    """
    if isinstance(value, list):
        return sum(value)
    return value


def cache_type(cache, access_type):
    data = cache[access_type]

    hit = scalar_or_sum(data["hit"])
    miss = scalar_or_sum(data["miss"])
    merge = scalar_or_sum(data["mshr_merge"])

    return {
        "hit": hit,
        "miss": miss,
        "access": hit + miss,
        "mshr_merge": merge,
    }


def parse_file(path):
    with path.open() as f:
        root = json.load(f)

    if not isinstance(root, list) or not root:
        raise RuntimeError(
            f"{path}: unexpected ChampSim JSON root"
        )

    # One phase object for our single simulation.
    phase = root[0]

    if "roi" not in phase:
        raise RuntimeError(f"{path}: missing ROI statistics")

    roi = phase["roi"]

    # ---------- Core ----------
    cores = roi["cores"]

    if len(cores) != 1:
        raise RuntimeError(
            f"{path}: expected exactly one core, found {len(cores)}"
        )

    core = cores[0]

    instructions = int(core["instructions"])
    cycles = int(core["cycles"])
    ipc = safe_div(instructions, cycles)

    # ---------- L2 ----------
    l2 = roi["cpu0_L2C"]

    load = cache_type(l2, "LOAD")
    rfo = cache_type(l2, "RFO")
    pf_access = cache_type(l2, "PREFETCH")
    write = cache_type(l2, "WRITE")

    # Keep both definitions so we do not silently hide whether stores/RFOs
    # are included in a later project plot.
    l2_load_misses = load["miss"]

    l2_demand_misses = (
        load["miss"]
        + rfo["miss"]
    )

    l2_load_mpki = (
        1000.0 * l2_load_misses / instructions
        if instructions
        else 0.0
    )

    l2_demand_mpki = (
        1000.0 * l2_demand_misses / instructions
        if instructions
        else 0.0
    )

    pf_requested = int(l2["prefetch requested"])
    pf_issued = int(l2["prefetch issued"])
    pf_filled = int(l2["prefetch filled"])
    pf_useful = int(l2["useful prefetch"])
    pf_useless = int(l2["useless prefetch"])

    pf_accuracy = safe_div(
        pf_useful,
        pf_filled
    )

    pf_fill_rate = safe_div(
        pf_filled,
        pf_issued
    )

    # A filled prefetch may still reside in cache at end-of-ROI, so this
    # quantity is descriptive only, not an error check.
    pf_unclassified_at_end = (
        pf_filled
        - pf_useful
        - pf_useless
    )

    # ---------- LLC ----------
    llc = roi["LLC"]

    llc_load = cache_type(llc, "LOAD")
    llc_rfo = cache_type(llc, "RFO")
    llc_pf = cache_type(llc, "PREFETCH")
    llc_write = cache_type(llc, "WRITE")

    # ---------- DRAM ----------
    dram_entries = roi["DRAM"]

    # roi["DRAM"] is a vector of channels.
    if isinstance(dram_entries, dict):
        dram_entries = [dram_entries]

    dram_read_hit = 0
    dram_read_miss = 0
    dram_write_hit = 0
    dram_write_miss = 0

    for channel in dram_entries:
        dram_read_hit += int(
            channel["RQ ROW_BUFFER_HIT"]
        )
        dram_read_miss += int(
            channel["RQ ROW_BUFFER_MISS"]
        )
        dram_write_hit += int(
            channel["WQ ROW_BUFFER_HIT"]
        )
        dram_write_miss += int(
            channel["WQ ROW_BUFFER_MISS"]
        )

    dram_reads = (
        dram_read_hit
        + dram_read_miss
    )

    dram_writes = (
        dram_write_hit
        + dram_write_miss
    )

    dram_traffic = (
        dram_reads
        + dram_writes
    )

    result = {
        "json_file": str(path),

        "instructions": instructions,
        "cycles": cycles,
        "ipc": ipc,

        "l2_load_accesses": load["access"],
        "l2_load_hits": load["hit"],
        "l2_load_misses": load["miss"],
        "l2_load_mshr_merges": load["mshr_merge"],

        "l2_rfo_accesses": rfo["access"],
        "l2_rfo_hits": rfo["hit"],
        "l2_rfo_misses": rfo["miss"],

        "l2_load_mpki": l2_load_mpki,
        "l2_demand_misses": l2_demand_misses,
        "l2_demand_mpki": l2_demand_mpki,

        "pf_requested": pf_requested,
        "pf_issued": pf_issued,
        "pf_filled": pf_filled,
        "pf_useful": pf_useful,
        "pf_useless": pf_useless,

        "pf_accuracy": pf_accuracy,
        "pf_fill_rate": pf_fill_rate,
        "pf_unclassified_at_end":
            pf_unclassified_at_end,

        "l2_prefetch_accesses":
            pf_access["access"],
        "l2_prefetch_hits":
            pf_access["hit"],
        "l2_prefetch_misses":
            pf_access["miss"],

        "llc_load_accesses":
            llc_load["access"],
        "llc_load_misses":
            llc_load["miss"],

        "llc_rfo_accesses":
            llc_rfo["access"],
        "llc_rfo_misses":
            llc_rfo["miss"],

        "llc_prefetch_accesses":
            llc_pf["access"],
        "llc_prefetch_misses":
            llc_pf["miss"],

        "llc_write_accesses":
            llc_write["access"],

        "dram_read_row_hits":
            dram_read_hit,
        "dram_read_row_misses":
            dram_read_miss,
        "dram_reads":
            dram_reads,

        "dram_write_row_hits":
            dram_write_hit,
        "dram_write_row_misses":
            dram_write_miss,
        "dram_writes":
            dram_writes,

        "dram_total_traffic":
            dram_traffic,
    }

    return result


def print_summary(r):
    print("========================================")
    print("ChampSim ROI summary")
    print("========================================")

    print(f"Instructions             : {r['instructions']}")
    print(f"Cycles                   : {r['cycles']}")
    print(f"IPC                      : {r['ipc']:.6f}")

    print()
    print("L2 demand")
    print("----------------------------------------")
    print(f"LOAD accesses            : {r['l2_load_accesses']}")
    print(f"LOAD hits                : {r['l2_load_hits']}")
    print(f"LOAD misses              : {r['l2_load_misses']}")
    print(f"LOAD MPKI                : {r['l2_load_mpki']:.4f}")

    print(f"RFO accesses             : {r['l2_rfo_accesses']}")
    print(f"RFO misses               : {r['l2_rfo_misses']}")

    print(f"LOAD+RFO misses          : {r['l2_demand_misses']}")
    print(f"LOAD+RFO MPKI            : {r['l2_demand_mpki']:.4f}")

    print()
    print("L2 prefetch")
    print("----------------------------------------")
    print(f"Requested                : {r['pf_requested']}")
    print(f"Issued                   : {r['pf_issued']}")
    print(f"Filled                   : {r['pf_filled']}")
    print(f"Useful                   : {r['pf_useful']}")
    print(f"Useless                  : {r['pf_useless']}")

    if r["pf_filled"]:
        print(
            f"Accuracy useful/filled   : "
            f"{100*r['pf_accuracy']:.2f}%"
        )
    else:
        print("Accuracy useful/filled   : N/A")

    print(
        f"Filled but not classified: "
        f"{r['pf_unclassified_at_end']}"
    )

    print()
    print("DRAM traffic")
    print("----------------------------------------")
    print(f"Reads                    : {r['dram_reads']}")
    print(f"Writes                   : {r['dram_writes']}")
    print(f"Total transactions       : {r['dram_total_traffic']}")


def main():
    parser = argparse.ArgumentParser(
        description="Parse ChampSim JSON ROI statistics."
    )

    parser.add_argument(
        "json_file",
        type=Path
    )

    parser.add_argument(
        "--csv",
        type=Path,
        help="Optional one-row CSV output"
    )

    args = parser.parse_args()

    result = parse_file(args.json_file)

    print_summary(result)

    if args.csv is not None:
        args.csv.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with args.csv.open(
            "w",
            newline=""
        ) as f:
            writer = csv.DictWriter(
                f,
                fieldnames=list(result.keys())
            )

            writer.writeheader()
            writer.writerow(result)

        print()
        print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()
