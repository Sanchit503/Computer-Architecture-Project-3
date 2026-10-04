#!/usr/bin/env bash

set -uo pipefail

ROOT="$HOME/CA_Project"

WARMUP=50000000
SIM=100000000
DUMP_LIMIT=1000000

PARSER="$ROOT/scripts/parse_champsim.py"
CHAR="$ROOT/scripts/characterize_memory_v3.py"

declare -A TRACE

TRACE[bwaves]="603.bwaves_s-891B.champsimtrace.xz"
TRACE[mcf]="605.mcf_s-1554B.champsimtrace.xz"
TRACE[lbm]="619.lbm_s-4268B.champsimtrace.xz"
TRACE[omnetpp]="620.omnetpp_s-141B.champsimtrace.xz"
TRACE[xalancbmk]="623.xalancbmk_s-165B.champsimtrace.xz"
TRACE[fotonik3d]="649.fotonik3d_s-1176B.champsimtrace.xz"

WORKLOADS=(
    bwaves
    mcf
    lbm
    omnetpp
    xalancbmk
    fotonik3d
)

CONFIGS=(
    nopref
    ip_stride
    spp
    ampm
)

binary_for()
{
    case "$1" in
        nopref)    echo "$ROOT/ChampSim/bin/p3_final_nopref" ;;
        ip_stride) echo "$ROOT/ChampSim/bin/p3_final_ip_stride" ;;
        spp)       echo "$ROOT/ChampSim/bin/p3_final_spp" ;;
        ampm)      echo "$ROOT/ChampSim/bin/p3_final_ampm" ;;
        *) return 1 ;;
    esac
}

run_one()
{
    local workload="$1"
    local config="$2"

    local trace="$ROOT/traces/${TRACE[$workload]}"
    local binary
    binary="$(binary_for "$config")"

    local outdir="$ROOT/results/official/spec/$workload/$config"

    local json="$outdir/result.json"
    local log="$outdir/run.log"
    local csv="$outdir/result.csv"
    local done_file="$outdir/DONE"
    local fail_file="$outdir/FAILED"
    local meta="$outdir/metadata.txt"

    mkdir -p "$outdir"

    if [[ -f "$done_file" && -s "$json" && -s "$csv" ]]; then
        echo "SKIP: $workload / $config already complete"
        return 0
    fi

    rm -f "$fail_file"

    if [[ ! -x "$binary" ]]; then
        echo "ERROR: binary missing: $binary"
        return 1
    fi

    if [[ ! -s "$trace" ]]; then
        echo "ERROR: trace missing: $trace"
        return 1
    fi

    {
        echo "workload=$workload"
        echo "config=$config"
        echo "trace=$trace"
        echo "binary=$binary"
        echo "warmup=$WARMUP"
        echo "simulation=$SIM"
        echo "started=$(date -Iseconds)"
        echo "champsim_commit=$(git -C "$ROOT/ChampSim" rev-parse HEAD)"
        echo "binary_sha256=$(sha256sum "$binary" | awk '{print $1}')"
        echo "trace_sha256=$(sha256sum "$trace" | awk '{print $1}')"
    } > "$meta"

    echo
    echo "============================================================"
    echo "START: $workload / $config"
    echo "TIME : $(date -Iseconds)"
    echo "============================================================"

    if [[ "$config" == "nopref" ]]; then
        local dump="$ROOT/results/official/characterization/$workload/l2_first_1M.csv"

        mkdir -p "$(dirname "$dump")"
        rm -f "$dump"

        CHAMPSIM_L2_DUMP="$dump" \
        CHAMPSIM_L2_DUMP_LIMIT="$DUMP_LIMIT" \
        "$binary" \
            --warmup-instructions "$WARMUP" \
            --simulation-instructions "$SIM" \
            --json "$json" \
            "$trace" \
            > "$log" 2>&1

        status=$?
    else
        unset CHAMPSIM_L2_DUMP || true
        unset CHAMPSIM_L2_DUMP_LIMIT || true

        "$binary" \
            --warmup-instructions "$WARMUP" \
            --simulation-instructions "$SIM" \
            --json "$json" \
            "$trace" \
            > "$log" 2>&1

        status=$?
    fi

    echo "finished=$(date -Iseconds)" >> "$meta"
    echo "exit_status=$status" >> "$meta"

    if [[ "$status" -ne 0 ]]; then
        echo "$status" > "$fail_file"
        echo "FAILED: $workload / $config status=$status"
        return 1
    fi

    if ! grep -q 'Simulation finished CPU 0' "$log"; then
        echo "simulation completion marker missing" > "$fail_file"
        echo "FAILED: completion marker missing"
        return 1
    fi

    if [[ ! -s "$json" ]]; then
        echo "JSON missing" > "$fail_file"
        echo "FAILED: JSON missing"
        return 1
    fi

    python3 -m json.tool "$json" > /dev/null

    if [[ $? -ne 0 ]]; then
        echo "invalid JSON" > "$fail_file"
        echo "FAILED: invalid JSON"
        return 1
    fi

    python3 "$PARSER" \
        "$json" \
        --csv "$csv" \
        > "$outdir/parser.txt"

    if [[ $? -ne 0 ]]; then
        echo "parser failure" > "$fail_file"
        echo "FAILED: parser"
        return 1
    fi

    if [[ "$config" == "nopref" ]]; then
        local dump="$ROOT/results/official/characterization/$workload/l2_first_1M.csv"
        local analysis="$ROOT/results/official/characterization/$workload/analysis_v3"

        if [[ ! -s "$dump" ]]; then
            echo "characterization dump missing" > "$fail_file"
            echo "FAILED: dump missing"
            return 1
        fi

        local rows
        rows=$(( $(wc -l < "$dump") - 1 ))

        echo "characterization_rows=$rows" >> "$meta"

        mkdir -p "$analysis"

        python3 "$CHAR" \
            "$dump" \
            --outdir "$analysis" \
            --top 25 \
            --min-pc-accesses 1000 \
            > "$analysis/report.txt"

        if [[ $? -ne 0 ]]; then
            echo "characterization failure" > "$fail_file"
            echo "FAILED: characterization"
            return 1
        fi
    fi

    touch "$done_file"

    echo "PASS: $workload / $config"
    echo "TIME: $(date -Iseconds)"
}

MAX_JOBS="${MAX_JOBS:-4}"

echo "============================================================"
echo "PROJECT 3 OFFICIAL SPEC RUNS -- PARALLEL"
echo "============================================================"
echo "Warmup       : $WARMUP"
echo "Measured     : $SIM"
echo "L2 dump limit: $DUMP_LIMIT"
echo "Host workers : $MAX_JOBS"
echo

declare -A JOB_LABEL
ACTIVE=0
FAILED_JOBS=0

launch_job()
{
    local workload="$1"
    local config="$2"

    run_one "$workload" "$config" &
    local pid=$!

    JOB_LABEL["$pid"]="$workload / $config"
    ACTIVE=$((ACTIVE + 1))

    echo "LAUNCHED [$pid]: $workload / $config"
}

reap_one()
{
    local done_pid=""

    if wait -n -p done_pid; then
        echo "WORKER COMPLETE [$done_pid]: ${JOB_LABEL[$done_pid]:-unknown}"
    else
        echo "WORKER FAILED [$done_pid]: ${JOB_LABEL[$done_pid]:-unknown}"
        FAILED_JOBS=$((FAILED_JOBS + 1))
    fi

    if [[ -n "$done_pid" ]]; then
        unset 'JOB_LABEL['"$done_pid"']'
    fi

    ACTIVE=$((ACTIVE - 1))
}

for workload in "${WORKLOADS[@]}"; do
    for config in "${CONFIGS[@]}"; do
        while (( ACTIVE >= MAX_JOBS )); do
            reap_one
        done

        launch_job "$workload" "$config"
    done
done

while (( ACTIVE > 0 )); do
    reap_one
done

echo
echo "============================================================"

if (( FAILED_JOBS == 0 )); then
    echo "ALL SPEC EXPERIMENTS COMPLETE"
    exit 0
else
    echo "$FAILED_JOBS SPEC EXPERIMENT(S) FAILED"
    exit 1
fi
