#!/usr/bin/env python3

import argparse
import csv
import fcntl
import hashlib
import json
import os
import subprocess
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results/official/course"

WARMUP = 50_000_000
SIM = 100_000_000
WORKERS = 8

EXPECTED_COMMIT = (
    "667475ef1397817e2257edcf69f9ce778850d0aa"
)

CONFIGS = {
    "ip_stride": "p3_final_ip_stride",
    "spp": "p3_final_spp",
    "ampm": "p3_final_ampm",
}

WORKLOADS = {
    "bfscc",
    "pagerankdelta",
    "triangle",
    "llama2",
}

PARSER = ROOT / "scripts/parse_champsim.py"
MANIFEST = ROOT / "manifests/final_course_trace_selection.json"


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(
            lambda: f.read(4 * 1024 * 1024), b""
        ):
            h.update(block)
    return h.hexdigest()


def check_hash_manifest(filename):
    p = ROOT / "manifests" / filename

    r = subprocess.run(
        ["sha256sum", "-c", str(p)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    if r.returncode != 0:
        raise RuntimeError(
            f"Hash verification failed:\n{r.stdout}\n{r.stderr}"
        )

    print(r.stdout, end="", flush=True)


def verify_result(out):
    required = [
        out / "DONE",
        out / "result.json",
        out / "result.csv",
        out / "run.log",
        out / "parser.txt",
        out / "metadata.json",
    ]

    if not all(
        p.is_file() and (p.name == "DONE" or p.stat().st_size > 0)
        for p in required
    ):
        return False

    if (out / "FAILED").exists():
        return False

    try:
        with (out / "result.json").open() as f:
            json.load(f)

        with (out / "result.csv").open() as f:
            r = next(csv.DictReader(f))

        if int(r["instructions"]) < SIM:
            return False

        if float(r["ipc"]) <= 0:
            return False

        with (out / "metadata.json").open() as f:
            meta = json.load(f)

        if meta["warmup"] != WARMUP:
            return False

        if meta["simulation"] != SIM:
            return False

        if meta["champsim_commit"] != EXPECTED_COMMIT:
            return False

        with (out / "run.log").open(
            errors="replace"
        ) as f:
            if not any(
                "Simulation finished CPU 0" in line
                for line in f
            ):
                return False

        return True

    except (OSError, ValueError, KeyError,
            StopIteration, TypeError):
        return False


def preflight():
    print("===== CHAMPSIM COMMIT =====", flush=True)

    commit = subprocess.check_output(
        ["git", "-C", str(ROOT / "ChampSim"),
         "rev-parse", "HEAD"],
        text=True,
    ).strip()

    if commit != EXPECTED_COMMIT:
        raise RuntimeError("ChampSim commit mismatch")

    print(commit, flush=True)

    print("===== BINARIES =====", flush=True)
    check_hash_manifest("final_measurement_binaries.sha256")

    print("===== CONFIGS =====", flush=True)
    check_hash_manifest("final_config_hashes.sha256")

    if not PARSER.is_file():
        raise RuntimeError("Missing ChampSim parser")

    with MANIFEST.open() as f:
        manifest = json.load(f)

    records = manifest["traces"]
    names = [r["workload"] for r in records]

    if len(records) != 4 or set(names) != WORKLOADS:
        raise RuntimeError("Unexpected workload selection")

    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate selected workload")

    jobs = []

    print("===== SELECTED TRACES =====", flush=True)

    for record in records:
        workload = record["workload"]

        trace = ROOT / record["trace"]

        if not trace.is_file():
            raise RuntimeError(f"Missing trace: {trace}")

        if sha256(trace) != record["sha256"]:
            raise RuntimeError(
                f"Trace checksum mismatch: {workload}"
            )

        baseline = OUT / workload / "nopref"

        if not (baseline / "DONE").is_file():
            raise RuntimeError(
                f"Missing baseline: {workload}"
            )

        with (baseline / "eligibility.json").open() as f:
            eligibility = json.load(f)

        if eligibility["llc_load_mpki"] < 1:
            raise RuntimeError(
                f"Ineligible workload: {workload}"
            )

        print(
            f"VERIFIED: {workload} "
            f"LLC_LOAD_MPKI="
            f"{eligibility['llc_load_mpki']:.3f}",
            flush=True,
        )

        for config, binary_name in CONFIGS.items():
            binary = ROOT / "ChampSim/bin" / binary_name

            if not binary.is_file():
                raise RuntimeError(
                    f"Missing binary: {binary}"
                )

            out = OUT / workload / config

            if (out / "DONE").exists():
                if not verify_result(out):
                    raise RuntimeError(
                        f"Invalid DONE result: {workload}/{config}"
                    )

                print(
                    f"SKIP VERIFIED: {workload}/{config}",
                    flush=True,
                )
                continue

            if out.exists() and any(out.iterdir()):
                raise RuntimeError(
                    f"Partial result exists: {out}\n"
                    "Inspect it before attempting a restart."
                )

            jobs.append(
                (workload, config, trace, binary, out,
                 record["sha256"])
            )

    print(f"\nPREFLIGHT PASS: {len(jobs)} jobs pending")
    return jobs


def run_one(job):
    workload, config, trace, binary, out, trace_hash = job

    out.mkdir(parents=True, exist_ok=True)

    result_json = out / "result.json"
    result_csv = out / "result.csv"
    run_log = out / "run.log"
    failed = out / "FAILED"

    metadata = {
        "workload": workload,
        "config": config,
        "trace": str(trace.relative_to(ROOT)),
        "trace_sha256": trace_hash,
        "binary": str(binary.relative_to(ROOT)),
        "binary_sha256": sha256(binary),
        "warmup": WARMUP,
        "simulation": SIM,
        "champsim_commit": EXPECTED_COMMIT,
        "started": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    try:
        (out / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )

        env = os.environ.copy()

        # Non-baseline runs must not create L2 dumps.
        env.pop("CHAMPSIM_L2_DUMP", None)
        env.pop("CHAMPSIM_L2_DUMP_LIMIT", None)

        cmd = [
            str(binary),
            "--warmup-instructions", str(WARMUP),
            "--simulation-instructions", str(SIM),
            "--json", str(result_json),
            str(trace),
        ]

        print(
            f"START: {workload}/{config}",
            flush=True,
        )

        with run_log.open("w") as f:
            subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
                check=True,
            )

        with run_log.open(errors="replace") as f:
            if not any(
                "Simulation finished CPU 0" in line
                for line in f
            ):
                raise RuntimeError(
                    "Simulation completion marker missing"
                )

        with result_json.open() as f:
            json.load(f)

        with (out / "parser.txt").open("w") as f:
            subprocess.run(
                [
                    "python3", str(PARSER),
                    str(result_json),
                    "--csv", str(result_csv),
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )

        with result_csv.open() as f:
            r = next(csv.DictReader(f))

        if int(r["instructions"]) < SIM:
            raise RuntimeError(
                "Incomplete measured instruction count"
            )

        if float(r["ipc"]) <= 0:
            raise RuntimeError("Invalid IPC")

        metadata["finished"] = datetime.now(
            timezone.utc
        ).isoformat()

        metadata["status"] = "complete"

        (out / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )

        (out / "DONE").touch()

        print(
            f"PASS: {workload}/{config}",
            flush=True,
        )

        return True

    except Exception:
        failed.write_text(traceback.format_exc())

        print(
            f"FAILED: {workload}/{config}",
            flush=True,
        )

        return False


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--check-only",
        action="store_true",
        help="Validate inputs without launching simulations",
    )

    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    # Prevent two copies of this runner from competing.
    with (OUT / ".prefetch_runner.lock").open("a+") as lock:
        try:
            fcntl.flock(
                lock,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            raise SystemExit(
                "Another course prefetcher runner is active"
            )

        jobs = preflight()

        if args.check_only:
            print("CHECK ONLY: no simulations launched")
            return

        print("=" * 65)
        print("OFFICIAL GRAPH/AI PREFETCHER RUNS")
        print("=" * 65)
        print(f"Workers: {WORKERS}")
        print(f"Warmup: {WARMUP}")
        print(f"Measured: {SIM}")
        print(f"Pending jobs: {len(jobs)}")

        failures = 0

        with ThreadPoolExecutor(
            max_workers=WORKERS
        ) as pool:

            futures = [
                pool.submit(run_one, job)
                for job in jobs
            ]

            for future in as_completed(futures):
                if not future.result():
                    failures += 1

        if failures:
            raise SystemExit(
                f"FAILED EXPERIMENTS: {failures}"
            )

        print("ALL COURSE PREFETCHER RUNS COMPLETE")


if __name__ == "__main__":
    main()
