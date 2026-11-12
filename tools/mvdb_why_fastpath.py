"""Explain why the vectorised fast path is skipped.

The path probe shows that every query costs ~650 ms, which is the price of
the generic pure Python row scan, so the vectorised path never runs.  This
script reproduces the preconditions of ``Engine._fast_vector_select`` step by
step and prints the first one that fails.

Usage:
    python tools/mvdb_why_fastpath.py
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)
sys.path.insert(0, HERE)

import mvdb_bench as bench  # noqa: E402
from minivectordb import fastpath  # noqa: E402
from minivectordb.engine import Engine  # noqa: E402
from minivectordb.sqlparser import parse  # noqa: E402


def main():
    print("numpy available in this process :", fastpath.available(),
          "module:", fastpath.module())
    root = tempfile.mkdtemp(prefix="mvdb-why-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    engine = Engine(data_dir)
    engine.open()
    try:
        rows, dim, categories = 2000, 128, 8
        engine.execute("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(16), "
                       "content VARCHAR(64), embedding VECTOR(%d));" % dim)
        rng = random.Random(7)
        data = bench.build_dataset(rows, dim, categories, rng)
        for offset in range(0, len(data), 100):
            chunk = data[offset:offset + 100]
            values = ",".join(
                "(%d,'%s','%s','%s')" % (pk, category, content, bench.vector_literal(vec))
                for pk, category, content, vec in chunk)
            engine.execute("INSERT INTO docs VALUES %s;" % values)
        engine.execute("CREATE VECTOR INDEX docs_vec ON docs(embedding);")

        vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        literal = bench.vector_literal(vector)
        sql = ("SELECT id, L2_DISTANCE(embedding, '%s') AS distance FROM docs "
               "WHERE category = 'cat-0' ORDER BY distance LIMIT 10;" % literal)
        statement = parse(sql)
        table = engine._table(statement["table"])

        print("overlay entries                :", len(engine._overlay))
        print("order                          :", statement["order"])
        print("limit                          :", statement["limit"])
        target = engine._distance_target(table, statement, statement["order"][0][0])
        print("distance target                :",
              "None" if target is None else target[0])
        print("query text                    :",
              None if target is None else (target[2] or "")[:40])
        if target is not None:
            position, query_values, query_text = target
            nulls = table.store.null_mask(position)
            print("null mask any()                :",
                  None if nulls is None else bool(nulls.any()))
            arr = table.store.fixed_column_array(position)
            print("fixed_column_array              :",
                  None if arr is None else (arr.shape, arr.dtype))
            m64 = table.store.vector_matrix64(position)
            print("vector_matrix64                 :",
                  None if m64 is None else (m64.shape, m64.dtype))
            live = table.store.live_mask()
            pks = table.store.primary_keys()
            print("live / pks                      :",
                  None if live is None else live.shape,
                  None if pks is None else pks.shape)

        where = statement["where"]
        print("where node                     :", where)
        mask = engine._fast_filter_block(table, where, 0, min(64, rows))
        print("filter block mask               :",
              "None (fallback)" if mask is None else "%d/%d true" % (int(mask.sum()), mask.size))

        result = engine._fast_vector_select(table, statement)
        print("fast path result                :",
              "None (FALLBACK)" if result is None else "ok, %d rows" % len(result["rows"]))

        import time
        start = time.perf_counter()
        engine.execute(sql)
        print("generic execute                 : %.1f ms" % ((time.perf_counter() - start) * 1000))
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
