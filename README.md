# Computer Architecture Project 3

## Overview

This project evaluates hardware cache prefetching using ChampSim.

We compare four L2 cache configurations:
- No prefetcher
- IP-stride
- Signature Path Prefetching (SPP)
- AMPM

## Workloads

SPEC CPU 2017:
bwaves, mcf, lbm, omnetpp, xalancbmk, fotonik3d

Graph and AI:
BFSCC, PageRankDelta, Triangle, Llama2

## Experiment Settings

- Warmup: 50 million instructions
- Measurement: 100 million instructions
- Total workloads: 10
- Baseline configurations: 4
- Completed baseline simulations: 40

## Experimental Results

- data/midsem_40runs_corrected_spp.csv
- data/spp_original_vs_corrected.csv

The first dataset uses the corrected SPP results.
Original results are preserved separately.

## ChampSim Source

Upstream: https://github.com/CMU-SAFARI/ChampSim

Upstream base:
6513c5c45212ca677a9f046a51f278a70b0360f0

Frozen measurement commit:
667475ef1397817e2257edcf69f9ce778850d0aa

The four custom source patches are in
patches/champsim_series/.

The SPP bounds correction is in
patches/spp_queue_bounds.patch.

The patch series was verified to reconstruct
the exact frozen ChampSim source tree.

## Scripts

The scripts/ directory contains experiment runners,
result parsers, memory characterization tools,
and analysis/export utilities.

## Reproducibility

Trace-selection information and checksums are
recorded in manifests/.

Large traces, compiled binaries, raw simulation
outputs, and backups are not included in GitHub.

Traces must be obtained from authorized sources.

## Project Status

Midsem baseline experiments and corrected SPP
revalidation are complete.

Feedback-directed prefetcher optimization remains
part of the subsequent project work.
