"""Measure the resident memory of the query paths.

The judge runs the container with ``--memory=2g``.  The task requires the
database to serve a data set that is *larger* than the memory budget, so any
query path that materialises the whole table in RAM is a functional bug, not
just a performance problem: the case would die with an OOM kill and be
reported as "功能结果不符合要求".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mvdb_judge_sim import Db  # noqa: E402

LIMIT_BYTES = 2 * 1024 ** 3


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_bytes(pid: int) -> int:
    psapi = ctypes.WinDLL("psapi")
    kernel = ctypes.WinDLL("kernel32")
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    handle = kernel.OpenProcess(0x1000 | 0x0400, False, pid)   # QUERY_INFO | VM_READ
    try:
        if not handle:
            return 0
        try:
            psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return int(counters.PeakWorkingSetSize)
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return 0


def main():
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 300000
    dim = int(sys.argv[2]) if len(sys.argv) > 2 else 64
    work = tempfile.mkdtemp(prefix="mvdb-mem-")
    db = Db(work, os.path.join(work, "stderr.log"))
    print("rows=%d dim=%d  judge memory limit=2.0 GiB" % (rows, dim), flush=True)
    try:
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, grp BIGINT, v VECTOR(%d));" % dim)
        batch = 500
        started = time.monotonic()
        for start in range(0, rows, batch):
            values = []
            for i in range(start, min(start + batch, rows)):
                vec = [float((i * 7 + j) % 101) * 0.25 for j in range(dim)]
                values.append("(%d, %d, '%s')" % (
                    i + 1, i % 10, json.dumps(vec, separators=(",", ":"))))
            r = db.exec("INSERT INTO t VALUES %s;" % ", ".join(values))
            if r.get("ok") is not True:
                raise AssertionError("insert failed: %r" % (r,))
        print("insert done in %.1fs" % (time.monotonic() - started), flush=True)
        print("peak after insert: %.2f MiB" % (peak_bytes(db.proc.pid) / 1048576.0), flush=True)

        probe = json.dumps([0.5] * dim, separators=(",", ":"))
        r = db.exec("SELECT id, L2_DISTANCE(v, '%s') AS d FROM t WHERE grp = 3 "
                    "ORDER BY d ASC LIMIT 10;" % probe, "exact")
        if r.get("ok") is not True:
            raise AssertionError("topk failed: %r" % (r,))
        peak = peak_bytes(db.proc.pid)
        print("peak after TopK: %.2f MiB  (limit 2048 MiB)" % (peak / 1048576.0), flush=True)

        r = db.exec("SELECT id FROM t WHERE grp = 3;")
        if r.get("ok") is not True:
            raise AssertionError("select failed: %r" % (r,))
        peak = peak_bytes(db.proc.pid)
        print("peak after plain SELECT: %.2f MiB  (limit 2048 MiB)" % (peak / 1048576.0), flush=True)

        r = db.exec("SELECT id FROM t ORDER BY id LIMIT 5;")
        peak = peak_bytes(db.proc.pid)
        print("peak after ORDER BY full scan: %.2f MiB" % (peak / 1048576.0), flush=True)

        db.shutdown()
        verdict = "OK" if peak_bytes(db.proc.pid) else "see numbers"
        print("\nverdict: %s" % verdict, flush=True)
    except Exception as exc:
        print("ERROR %r" % (exc,), flush=True)
    finally:
        try:
            db.kill()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
