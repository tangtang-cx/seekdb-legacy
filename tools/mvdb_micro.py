"""Micro benchmark of the distance scan variants.

The scan dominates the query phase, so this script measures the variants
on the shape of the official benchmark (10,000 rows, 128 dimensions) and
reports the deviation from the reference distance as well.

Usage:
    python tools/mvdb_micro.py
"""
from __future__ import annotations

import math
import os
import sys
import time

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)

from minivectordb import fastpath  # noqa: E402
from minivectordb.core import l2_distance  # noqa: E402

ROWS = 10000
DIM = 128
K = 10
REPEAT = 50


def timeit(label, func, repeat=REPEAT):
    func()
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        func()
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    print("%-52s %8.3f ms" % (label, best * 1000))
    return best


def main():
    rng = numpy.random.default_rng(20261112)
    matrix32 = rng.uniform(-1.0, 1.0, size=(ROWS, DIM)).astype(numpy.float32)
    matrix64 = matrix32.astype(numpy.float64)
    query32 = rng.uniform(-1.0, 1.0, size=DIM).astype(numpy.float32)
    query64 = query32.astype(numpy.float64)
    pks = numpy.arange(1, ROWS + 1, dtype=numpy.int64)
    scratch = numpy.empty((4096, DIM), dtype=numpy.float64)

    print("== distance scan (10,000 x 128) ==")

    def current():
        out = None
        for start in range(0, ROWS, 4096):
            end = min(start + 4096, ROWS)
            out = fastpath.l2_distances_into(matrix64[start:end], query32,
                                             scratch[:end - start])
        return out

    def norm_formula():
        norms = numpy.einsum("ij,ij->i", matrix64, matrix64)
        qq = float(numpy.dot(query64, query64))
        out = None
        for start in range(0, ROWS, 4096):
            end = min(start + 4096, ROWS)
            d2 = norms[start:end] - 2.0 * (matrix64[start:end] @ query64) + qq
            numpy.maximum(d2, 0.0, out=d2)
            out = numpy.sqrt(d2)
        return out

    def float32_column(block_rows):
        def run():
            out = None
            for start in range(0, ROWS, block_rows):
                end = min(start + block_rows, ROWS)
                rows = end - start
                scratch[:rows] = matrix32[start:end]
                scratch[:rows] -= query64
                out = numpy.sqrt(numpy.einsum("ij,ij->i", scratch[:rows],
                                              scratch[:rows]))
            return out
        return run

    def float32_norm():
        norms = numpy.einsum("ij,ij->i", matrix32, matrix32).astype(numpy.float64)
        qq = float(numpy.dot(query32, query32))
        out = None
        for start in range(0, ROWS, 4096):
            end = min(start + 4096, ROWS)
            dot = (matrix32[start:end] @ query32).astype(numpy.float64)
            d2 = norms[start:end] - 2.0 * dot + qq
            numpy.maximum(d2, 0.0, out=d2)
            out = numpy.sqrt(d2)
        return out

    timeit("current: float64 mirror, subtract + einsum", current)
    timeit("norm formula on the float64 mirror", norm_formula)
    timeit("float32 column, per block float64 conversion (4096)", float32_column(4096))
    timeit("float32 column, per block float64 conversion (1024)", float32_column(1024))
    timeit("float32 column, norm formula in float32", float32_norm)

    reference = numpy.array([l2_distance(matrix32[i].tolist(), query32.tolist())
                             for i in range(200)])
    for label, function in (("float64 mirror", current),
                            ("norm formula", norm_formula),
                            ("float32 column", float32_column(1024)),
                            ("float32 norm", float32_norm)):
        values = function()[:200]
        print("%-52s max abs error %.3e"
              % (label, float(numpy.max(numpy.abs(values - reference)))))

    print()
    print("== top-k selection ==")
    distances = numpy.sqrt(numpy.sum((matrix64 - query64) ** 2, axis=1))

    def select():
        out = None
        for start in range(0, ROWS, 4096):
            end = min(start + 4096, ROWS)
            out = fastpath.select_topk(distances[start:end], pks[start:end], K)
        return out

    timeit("select_topk per chunk", select)
    return 0


if __name__ == "__main__":
    sys.exit(main())
