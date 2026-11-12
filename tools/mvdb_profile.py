"""Phase profile of MiniVectorDB: how the write and the query phase split.

The script measures the official benchmark shape in one process and reports
the wall clock of the write phase, the index phase and the query phase, so
that the effect of a change on either side is visible.

Usage:
    python tools/mvdb_profile.py [--rows 10000] [--queries 200]
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)
sys.path.insert(0, HERE)

import mvdb_bench as bench  # noqa: E402
from minivectordb.engine import Engine  # noqa: E402
from minivectordb.sqlparser import parse  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--categories", type=int, default=8)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261112)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    data = bench.build_dataset(args.rows, args.dim, args.categories, rng)
    root = tempfile.mkdtemp(prefix="mvdb-profile-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    engine = Engine(data_dir)
    engine.open()
    try:
        engine.execute("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), "
                       "content VARCHAR(64), embedding VECTOR(%d));" % args.dim)

        batch = data[:args.batch]
        sql = "INSERT INTO docs VALUES %s;" % ",".join(
            "(%d,'%s','%s','%s')" % (pk, category, content, bench.vector_literal(vec))
            for pk, category, content, vec in batch)
        start = time.perf_counter()
        for _ in range(5):
            parse(sql)
        parse_ms = (time.perf_counter() - start) / 5 * 1000
        print("parse 100-row INSERT            : %8.2f ms  (%.3f ms/row)"
              % (parse_ms, parse_ms / args.batch))

        write_start = time.perf_counter()
        for offset in range(0, len(data), args.batch):
            chunk = data[offset:offset + args.batch]
            values = ",".join(
                "(%d,'%s','%s','%s')" % (pk, category, content, bench.vector_literal(vec))
                for pk, category, content, vec in chunk)
            engine.execute("INSERT INTO docs VALUES %s;" % values)
        write_elapsed = time.perf_counter() - write_start
        print("write phase (%d rows)          : %8.3f s  (%.1f us/row)"
              % (len(data), write_elapsed, write_elapsed / len(data) * 1e6))

        index_start = time.perf_counter()
        engine.execute("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
        print("index phase                     : %8.3f s"
              % (time.perf_counter() - index_start))

        queries = []
        for index in range(args.queries):
            vector = [rng.uniform(-1.0, 1.0) for _ in range(args.dim)]
            if index % 2 == 0:
                queries.append("SELECT id, L2_DISTANCE(embedding, '%s') AS distance "
                               "FROM docs WHERE category = 'cat-%d' ORDER BY distance LIMIT 10;"
                               % (bench.vector_literal(vector), index % args.categories))
            else:
                queries.append("SELECT id, L2_DISTANCE(embedding, '%s') AS distance "
                               "FROM docs ORDER BY distance LIMIT 10;"
                               % bench.vector_literal(vector))
        query_start = time.perf_counter()
        for statement in queries:
            engine.execute(statement)
        query_elapsed = time.perf_counter() - query_start
        print("query phase (%d queries)        : %8.3f s  (%.3f ms/query)"
              % (len(queries), query_elapsed, query_elapsed / len(queries) * 1000))
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
