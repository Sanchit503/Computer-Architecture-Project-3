#!/usr/bin/env python3
"""Revalidate the remaining nine SPP workloads with a separate corrected binary.

Run from ~/CA_Project with --check-only first; launch with --workers 8.
Existing official results are read only. Completed validated jobs are skipped.
Interrupted/incomplete output directories require inspection before retry.
"""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / "CA_Project"
FIXED = Path.home() / "CA_Project_spp_fixed_release"
BINARY = FIXED / "ChampSim/bin/p3_spp_bounds_fixed"
CONFIG = FIXED / "spp_fixed.json"
PARSER = ROOT / "scripts/parse_champsim.py"
OUTROOT = ROOT / "results/validation/spp_bounds_fixed"

COMMIT = "667475ef1397817e2257edcf69f9ce778850d0aa"
BINARY_SHA256 = "824e7d43500bf14476d4ab54f5fededd3dc1f891114cdd87a727c19f294b211a"
WARMUP = 50_000_000
SIM = 100_000_000
WORKLOADS = (
    ("spec", "mcf"), ("spec", "lbm"), ("spec", "omnetpp"),
    ("spec", "xalancbmk"), ("spec", "fotonik3d"),
    ("course", "bfscc"), ("course", "pagerankdelta"),
    ("course", "triangle"), ("course", "llama2"),
)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def record(path):
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise ValueError(f"Expected one CSV row in {path}, found {len(rows)}")
    if int(rows[0]["instructions"]) < SIM or float(rows[0]["ipc"]) <= 0:
        raise ValueError(f"Invalid measured instruction count or IPC in {path}")
    return rows[0]


def original_trace(suite, workload):
    d = ROOT / "results/official" / suite / workload / "spp"
    if not (d / "DONE").is_file() or (d / "FAILED").exists():
        raise ValueError(f"Original experiment is not verified: {suite}/{workload}/spp")
    record(d / "result.csv")
    if suite == "spec":
        md = dict(line.split("=", 1) for line in (d / "metadata.txt").read_text().splitlines() if "=" in line)
    else:
        md = json.loads((d / "metadata.json").read_text())
    trace = Path(md["trace"]).expanduser()
    if not trace.is_absolute():
        trace = ROOT / trace
    if not trace.is_file() or trace.stat().st_size == 0:
        raise FileNotFoundError(f"Missing original trace: {trace}")
    return d, trace


def check_complete(d):
    if not (d / "DONE").is_file():
        return False
    if (d / "FAILED").exists():
        raise ValueError(f"Conflicting DONE and FAILED markers in {d}")
    for file in ("result.csv", "result.json", "run.log", "metadata.json", "parser.txt"):
        if not (d / file).is_file() or not (d / file).stat().st_size:
            raise ValueError(f"Incomplete DONE marker in {d}: {file}")
    json.loads((d / "result.json").read_text())
    record(d / "result.csv")
    if "Simulation finished CPU 0" not in (d / "run.log").read_text(errors="replace"):
        raise ValueError(f"Missing completion marker in {d}")
    meta = json.loads((d / "metadata.json").read_text())
    if meta.get("binary_sha256") != BINARY_SHA256 or meta.get("warmup") != WARMUP or meta.get("simulation") != SIM:
        raise ValueError(f"Invalid corrected-binary provenance in {d}")
    return True


def preflight():
    if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
        raise FileNotFoundError(f"Corrected SPP binary not executable: {BINARY}")
    if sha256(BINARY) != BINARY_SHA256:
        raise ValueError("Corrected SPP binary SHA-256 does not match the verified fingerprint")
    if not PARSER.is_file():
        raise FileNotFoundError(f"Missing parser: {PARSER}")
    original = json.loads((ROOT / "configs/project_spp.json").read_text())
    corrected = json.loads(CONFIG.read_text())
    if corrected.get("executable_name") != "p3_spp_bounds_fixed":
        raise ValueError("Wrong corrected executable name")
    original.pop("executable_name", None)
    corrected.pop("executable_name", None)
    if original != corrected:
        raise ValueError("Corrected configuration differs beyond executable name")
    git_commit = subprocess.check_output(["git", "-C", str(FIXED / "ChampSim"), "rev-parse", "HEAD"], text=True).strip()
    if git_commit != COMMIT:
        raise ValueError(f"Unexpected corrected source base commit: {git_commit}")
    changed = subprocess.check_output(["git", "-C", str(FIXED / "ChampSim"), "diff", "--name-only"], text=True).splitlines()
    if changed != ["prefetcher/spp_dev/spp_dev.cc"]:
        raise ValueError(f"Unexpected corrected source changes: {changed}")

    pending = []
    for suite, workload in WORKLOADS:
        _, trace = original_trace(suite, workload)
        d = OUTROOT / workload
        if check_complete(d):
            print(f"SKIP verified: {workload}", flush=True)
        elif d.exists() and any(d.iterdir()):
            raise RuntimeError(f"STOP: Unfinished prior attempt in {d}. Inspect/move it before retrying.")
        else:
            pending.append((suite, workload, trace))
            print(f"READY: {workload} ({suite})", flush=True)
    print(f"PREFLIGHT PASS: {len(pending)} pending, {len(WORKLOADS) - len(pending)} complete", flush=True)
    return pending


def run_one(job):
    suite, workload, trace = job
    d = OUTROOT / workload
    d.mkdir(parents=True, exist_ok=False)
    meta = {
        "variant": "spp_bounds_fixed", "suite": suite, "workload": workload,
        "trace": str(trace), "binary": str(BINARY), "binary_sha256": BINARY_SHA256,
        "champsim_base_commit": COMMIT, "warmup": WARMUP, "simulation": SIM,
        "started_utc": stamp(),
    }
    (d / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"START: {workload}", flush=True)
    env = os.environ.copy()
    env.pop("CHAMPSIM_L2_DUMP", None)
    env.pop("CHAMPSIM_L2_DUMP_LIMIT", None)
    try:
        cmd = [str(BINARY), "--warmup-instructions", str(WARMUP),
               "--simulation-instructions", str(SIM),
               "--json", str(d / "result.json"), str(trace)]
        with (d / "run.log").open("w") as log:
            status = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
        meta["exit_status"] = status
        meta["finished_utc"] = stamp()
        (d / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
        if status != 0:
            raise RuntimeError(f"Simulator exit code {status}")
        if "Simulation finished CPU 0" not in (d / "run.log").read_text(errors="replace"):
            raise RuntimeError("Simulation completion marker not found")
        json.loads((d / "result.json").read_text())
        with (d / "parser.txt").open("w") as txt:
            parsed = subprocess.run([sys.executable, str(PARSER), str(d / "result.json"),
                                     "--csv", str(d / "result.csv")],
                                    cwd=ROOT, stdout=txt, stderr=subprocess.STDOUT)
        if parsed.returncode != 0:
            raise RuntimeError(f"Parser exit code {parsed.returncode}")
        row = record(d / "result.csv")
        (d / "DONE").write_text(stamp() + "\n")
        print(f"PASS: {workload} IPC={float(row['ipc']):.6f}", flush=True)
        return workload, True
    except Exception as exc:
        (d / "FAILED").write_text(f"{stamp()} {type(exc).__name__}: {exc}\n")
        print(f"FAILED: {workload}: {exc}", flush=True)
        return workload, False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    if not 1 <= args.workers <= 8:
        ap.error("--workers must be between 1 and 8")
    OUTROOT.mkdir(parents=True, exist_ok=True)
    lock = (OUTROOT / ".batch.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("STOP: Another corrected-SPP batch is already active")
    try:
        pending = preflight()
        if args.check_only:
            print("CHECK ONLY: no simulations launched", flush=True)
            return 0
        if not pending:
            print("ALL NINE CORRECTED SPP VALIDATIONS ALREADY COMPLETE", flush=True)
            return 0
        print(f"LAUNCHING {len(pending)} SPP validations; workers={args.workers}", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            outcomes = [f.result() for f in as_completed([pool.submit(run_one, job) for job in pending])]
        failed = [w for w, ok in outcomes if not ok]
        if failed:
            print("BATCH FAILED: " + ", ".join(failed), flush=True)
            return 1
        print("ALL NINE CORRECTED SPP VALIDATIONS COMPLETE", flush=True)
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"PREFLIGHT ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
