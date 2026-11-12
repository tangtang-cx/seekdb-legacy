"""A/B benchmark of the vectorised path and the generic executor.

The generic executor is the reference implementation: it decodes every row
and evaluates the distance expression row by row.  The vectorised path has
to answer the same, but it must do it much faster.  Both run over the same
JSONL protocol, against the same data, in alternating rounds so that a
noisy machine hits both of them equally.

The benchmark shape is the one of the official task package: 10,000 rows of
128 dimensional vectors, 1,000 TopK queries with K = 10, half of them with
an equality filter on a normal column, and the write phase commits every
100 rows.

Usage:
    python tools/mvdb_bench_ab_e2e.py [--rows 10000] [--queries 1000] [--rounds 2]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)


def run_benchmark(disable_numpy, rows, queries, dim):
    """Run the benchmark with the vectorised path on or off."""

    environment = dict(os.environ)
    if disable_numpy:
        environment["MINIVECTORDB_DISABLE_NUMPY"] = "1"
    else:
        environment.pop("MINIVECTORDB_DISABLE_NUMPY", None)
    command = [
        sys.executable, "-c",
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import mvdb_bench\n"
        "class A: pass\n"
        "a = A()\n"
        "a.rows = %d; a.dim = %d; a.queries = %d; a.k = 10; a.batch = 100\n"
        "a.categories = 8; a.seed = 20261112; a.data_dir = ''; a.json = ''\n"
        "print(json.dumps(mvdb_bench.run(a)))\n" % (HERE, rows, dim, queries),
    ]
    finished = subprocess.run(command, capture_output=True, text=True, cwd=WS,
                              env=environment)
    if finished.returncode != 0:
        raise RuntimeError("benchmark failed: %s" % finished.stderr[-2000:])
    return json.loads(finished.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args(argv)

    results = {"generic": [], "vectorised": []}
    for round_number in range(args.rounds):
        for name, disable in (("generic", True), ("vectorised", False)):
            report = run_benchmark(disable, args.rows, args.queries, args.dim)
            results[name].append(report)
            print("round %d %-10s write %8.3f s   query %9.3f ms/query   "
                  "peak %6.2f MiB   data %7.1f MiB"
                  % (round_number, name, report["write_seconds"],
                     report["query_ms_per_query"], report["peak_working_set_mib"],
                     report["data_dir_bytes"] / (1 << 20)))

    print()
    summary = {}
    for name in ("generic", "vectorised"):
        reports = results[name]
        summary[name] = (min(r["write_seconds"] for r in reports),
                         min(r["query_ms_per_query"] for r in reports))
        print("%-10s write %8.3f s   query %9.3f ms/query"
              % (name, summary[name][0], summary[name][1]))
    generic_query = summary["generic"][1]
    vectorised_query = summary["vectorised"][1]
    print("the vectorised path is %.1fx faster on the query phase"
          % (generic_query / max(1e-9, vectorised_query)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
