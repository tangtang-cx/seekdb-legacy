"""Differential test of the vectorised path against the generic executor.

The vectorised path has to answer exactly like the generic executor, which
decodes every row and evaluates the distance expression row by row.  The
generic path can be selected with ``MINIVECTORDB_DISABLE_NUMPY``, so every
statement is executed twice and the two answers are compared.

Usage:
    python tools/mvdb_differential.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)

from minivectordb.core import SqlError  # noqa: E402
from minivectordb.engine import Engine  # noqa: E402


def run(engine, sql):
    try:
        response = engine.execute(sql)
    except SqlError as error:
        return ("error", error.code)
    except Exception as error:                       # noqa: BLE001
        return ("crash", type(error).__name__)
    return ("ok", response.get("columns"), response.get("rows"))


STATEMENTS = [
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3 OFFSET 2;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 10 OFFSET 5;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 4;",
    "SELECT id, L2_DISTANCE(v, '[0,0,0]') AS distance FROM d ORDER BY distance LIMIT 3;",
    "SELECT * FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2;",
    "SELECT id, cat FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE cat = 'a' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE cat <> 'a' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE cat = 'zzz' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE n > 30 ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE n >= 40 AND cat = 'c' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 5;",
    "SELECT id FROM d WHERE n = 40 ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 5;",
    "SELECT id FROM d WHERE id = 3 ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 5;",
    "SELECT id FROM d WHERE cat < 'b' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 5;",
    "SELECT id FROM d WHERE cat >= 'c' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 5;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') DESC LIMIT 3;",
    "SELECT id FROM d ORDER BY n LIMIT 3;",
    "SELECT id FROM empty_t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM empty_t WHERE cat = 'a' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d ORDER BY L2_DISTANCE('[0,0,0]', v) LIMIT 3;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 0;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0]') LIMIT 3;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, 'nonsense') LIMIT 3;",
    "SELECT id FROM d WHERE cat = 5 ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d WHERE n = 'x' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 100;",
    "SELECT id FROM d ORDER BY L2_DISTANCE(v, '[-1,0,0]') LIMIT 8;",
]


def main():
    root = tempfile.mkdtemp(prefix="mvdb-diff-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    engine = Engine(data_dir)
    engine.open()
    problems = 0
    checked = 0
    try:
        engine.execute("CREATE TABLE d (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                       "n BIGINT, v VECTOR(3));")
        engine.execute("CREATE TABLE empty_t (id BIGINT PRIMARY KEY, v VECTOR(3));")
        engine.execute("INSERT INTO d VALUES "
                       "(1,'a',10,'[1,0,0]'),(2,'b',20,'[0,1,0]'),(3,'a',30,'[0,0,1]'),"
                       "(4,'b',40,'[1,1,0]'),(5,'c',50,'[-1,0,0]'),(6,'c',60,'[2,0,0]'),"
                       "(7,'a',70,'[1,0,0]'),(8,'b',80,'[0.5,0.5,0]');")

        for sql in STATEMENTS:
            fast = run(engine, sql)
            os.environ["MINIVECTORDB_DISABLE_NUMPY"] = "1"
            generic = run(engine, sql)
            del os.environ["MINIVECTORDB_DISABLE_NUMPY"]
            checked += 1
            if fast != generic:
                problems += 1
                print("DIFFERENT: %s" % sql)
                print("   vectorised: %r" % (fast,))
                print("   generic   : %r" % (generic,))

        # A mutation between two queries must be visible to both paths.
        for sql in ("SELECT id FROM d ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;",
                    "SELECT id FROM d WHERE cat = 'a' ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 3;"):
            engine.execute("UPDATE d SET v = '[9,9,9]' WHERE id = 1;")
            fast = run(engine, sql)
            os.environ["MINIVECTORDB_DISABLE_NUMPY"] = "1"
            generic = run(engine, sql)
            del os.environ["MINIVECTORDB_DISABLE_NUMPY"]
            checked += 1
            if fast != generic:
                problems += 1
                print("DIFFERENT after an update: %s" % sql)
                print("   vectorised: %r" % (fast,))
                print("   generic   : %r" % (generic,))
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)

    print("\n%d statements compared, %d differences" % (checked, problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
