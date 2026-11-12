"""Locate the per-query cost of the fast vector path.

The benchmark reports ~660 ms per query, which is far slower than a numpy
scan of 10k x 128 vectors should be.  This probe separates the three
possible causes:

  * the vectorised path is not taken at all (the generic pure Python scan
    runs and costs ~1 s per query);
  * the vectorised path is taken but the filter forces a fallback;
  * the vectorised path is taken and is simply slow.

Usage:
    python tools/mvdb_path_probe.py [--rows 10000] [--dim 128]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mvdb_bench as bench  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--categories", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    data = bench.build_dataset(args.rows, args.dim, args.categories, rng)
    vector = [rng.uniform(-1.0, 1.0) for _ in range(args.dim)]
    literal = bench.vector_literal(vector)

    import tempfile
    root = tempfile.mkdtemp(prefix="mvdb-probe-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    db = bench.Db(data_dir, os.path.join(root, "stderr.log"))
    try:
        db.exec("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), "
                "content VARCHAR(64), embedding VECTOR(%d));" % args.dim)
        for offset in range(0, len(data), 100):
            chunk = data[offset:offset + 100]
            values = ",".join(
                "(%d,'%s','%s','%s')" % (pk, category, content, bench.vector_literal(vec))
                for pk, category, content, vec in chunk)
            db.exec("INSERT INTO docs VALUES %s;" % values)
        db.exec("CREATE VECTOR INDEX docs_vec ON docs(embedding);")

        cases = {
            "plain_order": ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance "
                            "FROM docs ORDER BY distance LIMIT 10;" % literal),
            "filtered": ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance "
                         "FROM docs WHERE category = 'cat-0' ORDER BY distance LIMIT 10;"
                         % literal),
            "pk_filter": ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance "
                          "FROM docs WHERE id = 4242 ORDER BY distance LIMIT 10;"
                          % literal),
            "count_only": "SELECT COUNT(*) FROM docs;",
            "no_vec": "SELECT id FROM docs WHERE category = 'cat-0' LIMIT 10;",
        }
        for name, sql in cases.items():
            # warm up
            db.exec(sql)
            best = None
            for _ in range(args.repeat):
                start = time.perf_counter()
                response = db.exec(sql)
                elapsed = time.perf_counter() - start
                best = elapsed if best is None else min(best, elapsed)
            print("%-14s %8.2f ms  rows=%d" % (
                name, best * 1000, len(response.get("rows") or [])))
    finally:
        try:
            db.shutdown()
        except Exception:
            pass
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
