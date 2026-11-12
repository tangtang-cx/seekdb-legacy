"""Semantics regression for the vectorised path against the generic executor.

The vectorised path keeps a float64 copy of the vector column in memory, so
a mutation that does not change the file size (an UPDATE of a vector) can
leave a stale copy behind.  This script runs the statement shapes that
expose such a stale copy and checks the answers.

Usage:
    python tools/mvdb_semantics.py
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)

from minivectordb.core import SqlError  # noqa: E402
from minivectordb.engine import Engine  # noqa: E402

FAILURES: list[str] = []


def check(name, got, expected):
    ok = got == expected
    if isinstance(got, list) and isinstance(expected, list):
        ok = len(got) == len(expected) and all(
            (abs(a - b) <= 1e-5 * max(1.0, abs(b))) if isinstance(a, float) and isinstance(b, float)
            else a == b for a, b in zip(got, expected))
    print("%-52s %s" % (name, "PASS" if ok else "FAIL"))
    if not ok:
        print("    got     : %r" % (got,))
        print("    expected: %r" % (expected,))
        FAILURES.append(name)


def main():
    root = tempfile.mkdtemp(prefix="mvdb-sem-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    engine = Engine(data_dir)
    engine.open()
    try:
        engine.execute("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(3));")

        def ids(sql):
            try:
                response = engine.execute(sql)
            except SqlError as error:
                return "error:%s" % error.code
            return [row[0] for row in response.get("rows") or []]

        # -- 1. plain TopK --------------------------------------------------
        engine.execute("INSERT INTO t VALUES (1,'a','[1,0,0]'),(2,'b','[0,1,0]'),"
                       "(3,'a','[0,0,1]'),(4,'b','[3,0,0]');")
        # id1 is at distance 0; id2, id3 and id4 are at sqrt(2), sqrt(2) and
        # sqrt(5), so the second row is a tie that the primary key breaks.
        check("topk after insert",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [1, 2])

        # -- 2. UPDATE of a vector, same session ----------------------------
        engine.execute("UPDATE t SET v = '[0,0,1]' WHERE id = 1;")
        # id1 now ties with id2 and id3, the primary key breaks the tie.
        check("topk after vector UPDATE",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [1, 2])

        # -- 2b. an UPDATE that pushes a row out of the top k ---------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES (1,'a','[0,0,0]'),(2,'b','[5,0,0]'),"
                       "(3,'c','[6,0,0]');")
        engine.execute("UPDATE t SET v = '[100,0,0]' WHERE id = 1;")
        check("UPDATE pushes a row out of the top k",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2;"),
              [2, 3])

        # -- 2c. an UPDATE that pulls a row into the top k -------------------
        engine.execute("UPDATE t SET v = '[0,0,0]' WHERE id = 1;")
        check("UPDATE pulls a row into the top k",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2;"),
              [1, 2])

        # -- 2d. the same through a filtered query ---------------------------
        engine.execute("UPDATE t SET v = '[100,0,0]' WHERE id = 2;")
        check("UPDATE behind a filter",
              ids("SELECT id FROM t WHERE cat = 'b' "
                  "ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2;"),
              [2])

        # -- 3. the filtered query ------------------------------------------
        check("filtered topk after vector UPDATE",
              ids("SELECT id FROM t WHERE cat = 'b' "
                  "ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [2])

        # -- 4. a late INSERT -------------------------------------------------
        engine.execute("INSERT INTO t VALUES (5,'a','[1,0,0]');")
        # id5 is at distance 0, id1 at 1 and id3 at 5.
        check("topk after late INSERT",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [5, 1])

        # -- 5. DELETE -------------------------------------------------------
        engine.execute("DELETE FROM t WHERE id = 5;")
        check("topk after DELETE",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [1, 3])

        # -- 6. exact match: the distance has to be exactly zero ------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES (7,'x','[1.5,-2.5,3.5]'),(8,'y','[0,0,0]');")
        response = engine.execute("SELECT id, L2_DISTANCE(v, '[1.5,-2.5,3.5]') AS d "
                                  "FROM t ORDER BY d LIMIT 1;")
        check("exact match returns distance 0",
              [row[1] for row in response["rows"]], [0.0])
        check("exact match returns the right row", [row[0] for row in response["rows"]], [7])

        # -- 7. ties are broken by the primary key ---------------------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES (30,'x','[1,0,0]'),(10,'x','[1,0,0]'),"
                       "(20,'x','[1,0,0]');")
        check("tie break by primary key",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 3;"),
              [10, 20, 30])

        # -- 7b. many rows at the same distance ------------------------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES %s;" % ",".join(
            "(%d,'x','[1,0,0]')" % pk for pk in range(100, 0, -10)))
        check("tie break over many equal rows",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 4;"),
              [10, 20, 30, 40])
        check("tie break over many equal rows with a filter",
              ids("SELECT id FROM t WHERE cat = 'x' "
                  "ORDER BY L2_DISTANCE(v, '[1,0,0]') LIMIT 2;"),
              [10, 20])

        # -- 7c. rows with the same distance but different vectors -----------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES (1,'x','[1,0,0]'),(2,'y','[0,1,0]'),"
                       "(3,'z','[0,0,1]'),(4,'w','[-1,0,0]');")
        check("symmetric distances are ordered by primary key",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 4;"),
              [1, 2, 3, 4])

        # -- 8. the reported distance matches the reference -------------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES (1,'a','[1,2,3]'),(2,'b','[4,5,6]');")
        response = engine.execute("SELECT id, L2_DISTANCE(v, '[0,0,0]') AS d FROM t "
                                  "ORDER BY d LIMIT 2;")
        expected = sorted([math.sqrt(14.0), math.sqrt(77.0)])
        check("reported distances are exact",
              [round(row[1], 9) for row in response["rows"]], [round(v, 9) for v in expected])

        # -- 9. invalid vectors still raise the reference error --------------
        check("dimension mismatch is rejected",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,2]') LIMIT 1;"),
              "error:dimension_mismatch")
        check("non numeric vector is rejected",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[a,b,c]') LIMIT 1;"),
              "error:invalid_vector")

        # -- 10. quoted and unquoted literals agree --------------------------
        quoted = ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1,2,3]') LIMIT 1;")
        bare = ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, [1,2,3]) LIMIT 1;")
        check("quoted and unquoted literals agree", quoted, bare)
        check("quoted literal finds the right row", quoted, [1])

        # -- 11. LIMIT with an OFFSET ----------------------------------------
        engine.execute("DELETE FROM t;")
        engine.execute("INSERT INTO t VALUES %s;" % ",".join(
            "(%d,'x','[%d,0,0]')" % (pk, pk) for pk in range(1, 8)))
        check("limit with offset skips the first rows",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2 OFFSET 3;"),
              [4, 5])
        check("limit with offset and a filter",
              ids("SELECT id FROM t WHERE cat = 'x' "
                  "ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2 OFFSET 3;"),
              [4, 5])
        check("offset beyond the end returns nothing",
              ids("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 2 OFFSET 99;"),
              [])
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    print("all semantics checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
