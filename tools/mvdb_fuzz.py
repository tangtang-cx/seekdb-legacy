"""Random differential test of the vectorised path and the generic executor.

The vectorised TopK path has to answer exactly like the generic executor,
which decodes every row and evaluates the distance expression row by row.
The generic path can be selected with ``MINIVECTORDB_DISABLE_NUMPY``, so
every generated statement is executed twice and the two answers are
compared.

Usage:
    python tools/mvdb_fuzz.py [--queries 120] [--rows 400] [--dim 8] [--seed 7]
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)

from minivectordb.core import SqlError  # noqa: E402
from minivectordb.engine import Engine  # noqa: E402

CATEGORIES = ["a", "b", "cc", "dd", "e"]


def run(engine, sql):
    try:
        response = engine.execute(sql)
    except SqlError as error:
        return ("error", error.code)
    except Exception as error:                       # noqa: BLE001
        return ("crash", type(error).__name__)
    return ("ok", response.get("columns"), response.get("rows"))


def vector_literal(rng, dim):
    return "[" + ",".join("%.4f" % rng.uniform(-2.0, 2.0) for _ in range(dim)) + "]"


def build(engine, rng, rows, dim):
    engine.execute("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                   "n BIGINT, v VECTOR(%d));" % dim)
    for pk in range(1, rows + 1):
        category = rng.choice(CATEGORIES)
        number = rng.randint(0, 100)
        engine.execute("INSERT INTO t VALUES (%d,'%s',%d,'%s');"
                       % (pk, category, number, vector_literal(rng, dim)))


def random_statement(rng, dim):
    kind = rng.random()
    literal = vector_literal(rng, dim)
    if kind < 0.18:
        where = "WHERE cat = '%s'" % rng.choice(CATEGORIES)
    elif kind < 0.32:
        where = "WHERE n %s %d" % (rng.choice(["=", "<", ">", "<=", ">="]),
                                   rng.randint(0, 100))
    elif kind < 0.42:
        where = "WHERE cat = '%s' AND n > %d" % (rng.choice(CATEGORIES),
                                                  rng.randint(0, 100))
    elif kind < 0.48:
        where = "WHERE id = %d" % rng.randint(1, 20)
    else:
        where = ""
    order = rng.random()
    if order < 0.8:
        order_by = "ORDER BY L2_DISTANCE(v, '%s')" % literal
    else:
        order_by = "ORDER BY L2_DISTANCE(v, '%s') DESC" % literal
    limit = rng.choice([1, 3, 5, 10, 25])
    offset = rng.choice([0, 0, 0, 2, 7])
    projection = rng.choice(["id", "id, L2_DISTANCE(v, '%s') AS distance" % literal,
                              "*", "cat, n"])
    return ("SELECT %s FROM t %s %s LIMIT %d OFFSET %d;"
            % (projection, where, order_by, limit, offset))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=120)
    parser.add_argument("--rows", type=int, default=400)
    parser.add_argument("--dim", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    root = tempfile.mkdtemp(prefix="mvdb-fuzz-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    engine = Engine(data_dir)
    engine.open()
    mismatches = 0
    checked = 0
    try:
        rng = random.Random(args.seed)
        build(engine, rng, args.rows, args.dim)
        statements = [random_statement(rng, args.dim) for _ in range(args.queries)]
        for sql in statements:
            fast = run(engine, sql)
            os.environ["MINIVECTORDB_DISABLE_NUMPY"] = "1"
            generic = run(engine, sql)
            del os.environ["MINIVECTORDB_DISABLE_NUMPY"]
            checked += 1
            if fast != generic:
                mismatches += 1
                print("MISMATCH: %s" % sql)
                print("   vectorised: %r" % (fast,))
                print("   generic   : %r" % (generic,))
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)

    print("queries=%d mismatches=%d" % (checked, mismatches))
    print("fast path matches the generic executor on every query"
          if mismatches == 0 else "the two executors disagree")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
