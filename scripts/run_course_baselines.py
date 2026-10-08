#!/usr/bin/env python3

import csv
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path.home() / "CA_Project"
BIN = ROOT / "ChampSim/bin/p3_final_nopref"
PARSER = ROOT / "scripts/parse_champsim.py"
CHAR = ROOT / "scripts/characterize_memory_v3.py"

WARMUP = 50_000_000
SIM = 100_000_000
DUMP_LIMIT = 1_000_000

TRACES = {
    "bfscc":
        "ligra/ligra_BFSCC.com-lj.ungraph.gcc_6.3.0_O3.drop_15500M.length_250M.champsimtrace.xz",
    "pagerankdelta":
        "ligra/ligra_PageRankDelta.com-lj.ungraph.gcc_6.3.0_O3.drop_6000M.length_250M.champsimtrace.xz",
    "triangle":
        "ligra/ligra_Triangle.com-lj.ungraph.gcc_6.3.0_O3.drop_25000M.length_250M.champsimtrace.xz",
    "llama2":
        "llama2/llama2.c-stories15M.1.champsimtrace.gz",
}

TRACE_ROOT = ROOT / "traces/course_candidates"
RESULT_ROOT = ROOT / "results/official/course"


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_trace(workload):
    rel = Path(TRACES[workload])
    trace = TRACE_ROOT / rel

    if not trace.is_file() or trace.stat().st_size == 0:
        raise RuntimeError(f"Missing trace: {trace}")

    manifest_name = (
        "ml.sha256sum.txt"
        if workload == "llama2"
        else "ligra.sha256sum.txt"
    )

    manifest = ROOT / "manifests/course_traces" / manifest_name

    expected = None

    for line in manifest.read_text().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        recorded_name = parts[1].lstrip("*").strip()
        if Path(recorded_name).name == rel.name:
            expected = parts[0]
            break

    if expected is None:
        raise RuntimeError(f"No checksum for {rel.name}")

    actual = sha256(trace)

    if actual.lower() != expected.lower():
        raise RuntimeError(f"Trace checksum mismatch: {rel.name}")

    print(f"TRACE VERIFIED: {workload}", flush=True)
    return trace, actual


def run_one(workload, trace, trace_hash):
    out = RESULT_ROOT / workload / "nopref"
    out.mkdir(parents=True, exist_ok=True)

    done = out / "DONE"
    failed = out / "FAILED"
    result_json = out / "result.json"
    result_csv = out / "result.csv"
    log = out / "run.log"

    dump = (
        ROOT / "results/official/characterization"
        / workload / "l2_first_1M.csv"
    )

    analysis = dump.parent / "analysis_v3"

    if done.exists():
        required = [
            result_json, result_csv, dump,
            analysis / "summary.json",
            analysis / "report.txt",
        ]
        if all(p.is_file() and p.stat().st_size > 0 for p in required):
            print(f"SKIP: {workload} already completed", flush=True)
            return workload, "SKIPPED"
        raise RuntimeError(f"{workload}: incomplete DONE marker")

    if any(p.exists() for p in [result_json, result_csv, log]):
        raise RuntimeError(
            f"{workload}: partial results exist; inspect before retrying"
        )

    failed.unlink(missing_ok=True)

    try:
        dump.parent.mkdir(parents=True, exist_ok=True)
        analysis.mkdir(parents=True, exist_ok=True)

        metadata = {
            "workload": workload,
            "config": "nopref",
            "trace": str(trace),
            "trace_sha256": trace_hash,
            "binary": str(BIN),
            "binary_sha256": sha256(BIN),
            "warmup": WARMUP,
            "simulation": SIM,
            "dump_limit": DUMP_LIMIT,
            "champsim_commit": subprocess.check_output(
                ["git", "-C", str(ROOT / "ChampSim"),
                 "rev-parse", "HEAD"],
                text=True
            ).strip(),
        }

        (out / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )

        env = os.environ.copy()
        env["CHAMPSIM_L2_DUMP"] = str(dump)
        env["CHAMPSIM_L2_DUMP_LIMIT"] = str(DUMP_LIMIT)

        command = [
            str(BIN),
            "--warmup-instructions", str(WARMUP),
            "--simulation-instructions", str(SIM),
            "--json", str(result_json),
            str(trace),
        ]

        print(f"START: {workload} / nopref", flush=True)

        with log.open("w") as f:
            subprocess.run(
                command,
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
                check=True,
            )

        if "Simulation finished CPU 0" not in log.read_text(
            errors="replace"
        ):
            raise RuntimeError("Missing simulation completion marker")

        with result_json.open() as f:
            json.load(f)

        with (out / "parser.txt").open("w") as f:
            subprocess.run(
                ["python3", str(PARSER), str(result_json),
                 "--csv", str(result_csv)],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )

        if not dump.is_file():
            raise RuntimeError("Characterization dump missing")

        with dump.open() as f:
            sample_rows = sum(1 for _ in f) - 1

        if not (0 < sample_rows <= DUMP_LIMIT):
            raise RuntimeError(
                f"Invalid characterization sample count: {sample_rows}"
            )

        with (analysis / "report.txt").open("w") as f:
            subprocess.run(
                ["python3", str(CHAR), str(dump),
                 "--outdir", str(analysis),
                 "--top", "25",
                 "--min-pc-accesses", "1000"],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )

        if not (analysis / "summary.json").is_file():
            raise RuntimeError("Characterization summary missing")

        with result_csv.open(newline="") as f:
            r = next(csv.DictReader(f))

        instructions = int(r["instructions"])
        load = int(r["llc_load_misses"])
        rfo = int(r["llc_rfo_misses"])

        if instructions < SIM:
            raise RuntimeError("Measured instruction count too small")

        load_mpki = 1000 * load / instructions
        demand_mpki = 1000 * (load + rfo) / instructions

        eligibility = {
            "llc_load_mpki": load_mpki,
            "llc_demand_mpki": demand_mpki,
            "passes_load_only_threshold": load_mpki >= 1.0,
            "passes_demand_threshold": demand_mpki >= 1.0,
            "characterization_rows": sample_rows,
        }

        (out / "eligibility.json").write_text(
            json.dumps(eligibility, indent=2) + "\n"
        )

        done.touch()

        print(
            f"PASS: {workload} "
            f"LLC_LOAD_MPKI={load_mpki:.3f} "
            f"LLC_DEMAND_MPKI={demand_mpki:.3f}",
            flush=True,
        )

        return workload, "COMPLETE"

    except Exception as exc:
        failed.write_text(str(exc) + "\n")
        print(f"FAILED: {workload}: {exc}", flush=True)
        raise


def main():
    print("=" * 65)
    print("COURSE BASELINE EXPERIMENTS")
    print("=" * 65)
    print(f"Warmup: {WARMUP}")
    print(f"Measured: {SIM}")
    print("Workers: 4")

    # Verify everything BEFORE launching simulations.
    if not BIN.is_file():
        raise RuntimeError("Frozen binary missing")

    verified = {}
    for w in TRACES:
        verified[w] = check_trace(w)

    print("ALL TRACE CHECKSUMS VERIFIED", flush=True)

    failures = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = {
            executor.submit(run_one, w, *verified[w]): w
            for w in TRACES
        }

        for future in as_completed(jobs):
            try:
                future.result()
            except Exception as exc:
                failures.append((jobs[future], str(exc)))

    if failures:
        raise SystemExit(f"BASELINE FAILURES: {failures}")

    print("ALL FOUR BASELINE EXPERIMENTS COMPLETE")


if __name__ == "__main__":
    main()
