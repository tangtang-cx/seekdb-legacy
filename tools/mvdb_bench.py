"""Replica of the official performance benchmark for MiniVectorDB.

The task package freezes the shape of the benchmark (section 3.3):

  * 10,000 records, 128 dimensional vectors
  * 1,000 TopK queries with K = 10, half of them with an equality filter on a
    normal column
  * the write phase inserts 100 rows per batch and commits every batch
  * the query phase runs after the data is loaded and the index is ready
  * the platform measures wall clock time, disk I/O bytes and peak memory

This script reproduces that workload against a local process so the numbers can
be compared before and after a change.  The data distribution is our own; the
official one uses an unpublished random seed, so only the shape and the
magnitude of the numbers are comparable.

Usage:
    python tools/mvdb_bench.py [--rows 10000] [--queries 1000] [--dim 128]
                               [--k 10] [--data-dir DIR] [--json OUT]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
MAIN = os.path.join(WS, "main.py")
PYTHON = sys.executable


# --------------------------------------------------------------------------
# process counters (Windows)
# --------------------------------------------------------------------------
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _psapi():
    dll = ctypes.WinDLL("psapi.dll")
    kernel = ctypes.WinDLL("kernel32.dll")
    return dll, kernel


def counters(handle):
    """(read bytes, write bytes, peak working set) of a process handle."""

    dll, kernel = _psapi()
    io = _IO_COUNTERS()
    kernel.GetProcessIoCounters(handle, ctypes.byref(io))
    mem = _PROCESS_MEMORY_COUNTERS()
    mem.cb = ctypes.sizeof(mem)
    dll.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb)
    return (io.ReadTransferCount, io.WriteTransferCount, mem.PeakWorkingSetSize)


class Db:
    """One database process speaking the judge JSONL protocol."""

    def __init__(self, data_dir, log_path=None):
        self.data_dir = data_dir
        self.log = open(log_path, "ab", buffering=0) if log_path else subprocess.DEVNULL
        self.seq = 0
        self.proc = subprocess.Popen(
            [PYTHON, "-u", MAIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log, env=dict(os.environ, VECTORDB_DATA_DIR=data_dir))
        ready = self._readline()
        if ready != {"type": "ready"}:
            raise RuntimeError("expected ready, got %r" % (ready,))

    def _readline(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("database exited before response")
        return json.loads(line.decode("utf-8"))

    def exec(self, sql, mode="exact"):
        self.seq += 1
        request = {"id": self.seq, "op": "execute", "sql": sql, "search_mode": mode}
        self.proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        response = self._readline()
        if response.get("id") != request["id"]:
            raise RuntimeError("response id differs: %r" % (response,))
        return response

    def shutdown(self):
        self.seq += 1
        request = {"id": self.seq, "op": "shutdown"}
        self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        response = self._readline()
        self.proc.stdin.close()
        self.proc.wait(timeout=60)
        if self.log is not subprocess.DEVNULL:
            self.log.close()
        return response

    def handle(self):
        return self.proc._handle  # type: ignore[attr-defined]


def vector_literal(values):
    return "[" + ",".join("%.6f" % value for value in values) + "]"


def build_dataset(rows, dim, categories, rng):
    """Deterministic data with the same shape as the official workload."""

    data = []
    for index in range(rows):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        data.append((index + 1, "cat-%d" % (index % categories),
                     "payload-%d" % index, vector))
    return data


def run(args):
    rng = random.Random(args.seed)
    data = build_dataset(args.rows, args.dim, args.categories, rng)
    queries = []
    for index in range(args.queries):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(args.dim)]
        filtered = index % 2 == 0
        category = "cat-%d" % (index % args.categories)
        queries.append((vector, category if filtered else None))

    root = args.data_dir or tempfile.mkdtemp(prefix="mvdb-bench-")
    owned = args.data_dir is None
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(root, "stderr.log")

    db = Db(data_dir, log_path)
    try:
        db.exec("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), "
                "content VARCHAR(64), embedding VECTOR(%d));" % args.dim)

        write_start = time.perf_counter()
        for offset in range(0, len(data), args.batch):
            chunk = data[offset:offset + args.batch]
            values = ",".join(
                "(%d,'%s','%s','%s')" % (pk, category, content, vector_literal(vector))
                for pk, category, content, vector in chunk)
            response = db.exec("INSERT INTO docs VALUES %s;" % values)
            if not response.get("ok"):
                raise RuntimeError("insert failed: %r" % (response,))
        write_elapsed = time.perf_counter() - write_start

        # Index preparation is not timed by the platform, but its cost is
        # charged to the I/O and memory budget, so it is measured separately.
        index_start = time.perf_counter()
        db.exec("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
        index_elapsed = time.perf_counter() - index_start

        query_start = time.perf_counter()
        rows_returned = 0
        for vector, category in queries:
            if category is None:
                sql = ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance FROM docs "
                       "ORDER BY distance LIMIT %d;" % (vector_literal(vector), args.k))
            else:
                sql = ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance FROM docs "
                       "WHERE category = '%s' ORDER BY distance LIMIT %d;"
                       % (vector_literal(vector), category, args.k))
            response = db.exec(sql)
            if not response.get("ok"):
                raise RuntimeError("query failed: %r" % (response,))
            rows_returned += len(response.get("rows") or [])
        query_elapsed = time.perf_counter() - query_start

        handle = db.proc._handle
        read_bytes, write_bytes, peak = counters(handle)
        data_bytes = 0
        for base, _dirs, files in os.walk(data_dir):
            for name in files:
                try:
                    data_bytes += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
    finally:
        try:
            db.shutdown()
        except Exception:
            pass

    report = {
        "rows": args.rows,
        "dim": args.dim,
        "queries": args.queries,
        "k": args.k,
        "batch": args.batch,
        "write_seconds": round(write_elapsed, 3),
        "write_ms_per_batch": round(write_elapsed / max(1, len(data) // args.batch) * 1000, 2),
        "index_seconds": round(index_elapsed, 3),
        "query_seconds": round(query_elapsed, 3),
        "query_ms_per_query": round(query_elapsed / max(1, args.queries) * 1000, 3),
        "rows_returned": rows_returned,
        "process_read_bytes": read_bytes,
        "process_write_bytes": write_bytes,
        "peak_working_set_mib": round(peak / (1 << 20), 2),
        "data_dir_bytes": data_bytes,
    }
    print(json.dumps(report, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if owned:
        shutil.rmtree(root, ignore_errors=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--categories", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
