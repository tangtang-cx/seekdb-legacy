"""Probe the SQL surface required by the MiniVectorDB task.

The public package exercises only a narrow slice of the grammar.  The formal
set has 29 cases, and the official feedback says exactly one of them failed
with "功能结果不符合要求".  This probe walks the 13 published functional
requirements and reports every statement that the implementation rejects or
answers unexpectedly.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from mvdb_judge_sim import Db  # noqa: E402


# Each group is (requirement number, [(label, sql, expectation)])
# expectation: "ok"   -> must succeed
#              "fail" -> must fail (unsupported / invalid)
#              ("rows", expected_rows) -> must succeed with these rows
GROUPS = [
    ("R1 create table", [
        ("bigint+varchar", "CREATE TABLE a (id BIGINT PRIMARY KEY, c VARCHAR(32));", "ok"),
        ("vector column", "CREATE TABLE b (id BIGINT PRIMARY KEY, v VECTOR(3));", "ok"),
        ("two vector columns rejected", "CREATE TABLE c (id BIGINT PRIMARY KEY, v VECTOR(2), w VECTOR(2));", "fail"),
        ("no primary key rejected", "CREATE TABLE d (id BIGINT, c VARCHAR(4));", "fail"),
        ("two primary keys rejected", "CREATE TABLE e (id BIGINT PRIMARY KEY, k BIGINT PRIMARY KEY);", "fail"),
        ("duplicate table rejected", "CREATE TABLE a (id BIGINT PRIMARY KEY, c VARCHAR(4));", "fail"),
    ]),
    ("R2 insert/select/update/delete", [
        ("insert single", "INSERT INTO a VALUES (1, 'x');", "ok"),
        ("insert multi row", "INSERT INTO a VALUES (2, 'y'), (3, 'z');", "ok"),
        ("insert select", "CREATE TABLE a2 (id BIGINT PRIMARY KEY, c VARCHAR(32));", "ok"),
        ("insert ... select", "INSERT INTO a2 SELECT id, c FROM a;", "ok"),
        ("insert select rows", "SELECT id, c FROM a2 ORDER BY id;", ("rows", [[1, "x"], [2, "y"], [3, "z"]])),
        ("update by pk", "UPDATE a SET c = 'u' WHERE id = 1;", "ok"),
        ("update by condition", "UPDATE a SET c = 'v' WHERE c = 'y';", "ok"),
        ("delete by pk", "DELETE FROM a WHERE id = 3;", "ok"),
        ("delete by condition", "DELETE FROM a WHERE id >= 2;", "ok"),
        ("delete all", "DELETE FROM a;", "ok"),
    ]),
    ("R3 projection and predicates", [
        ("select star", "SELECT * FROM a2;", "ok"),
        ("select star columns", "SELECT * FROM a2;", ("columns", ["id", "c"])),
        ("comparison lt", "SELECT id FROM a2 WHERE id < 3;", "ok"),
        ("comparison le", "SELECT id FROM a2 WHERE id <= 2;", "ok"),
        ("comparison gt", "SELECT id FROM a2 WHERE id > 1;", "ok"),
        ("comparison ge", "SELECT id FROM a2 WHERE id >= 2;", "ok"),
        ("and", "SELECT id FROM a2 WHERE id >= 1 AND id <= 2;", "ok"),
        ("or", "SELECT id FROM a2 WHERE id = 1 OR id = 3;", "ok"),
        ("not equal", "SELECT id FROM a2 WHERE id <> 1;", "ok"),
        ("order by asc", "SELECT id FROM a2 ORDER BY id;", "ok"),
        ("order by desc", "SELECT id FROM a2 ORDER BY id DESC;", "ok"),
        ("order by two columns", "SELECT id FROM a2 ORDER BY c DESC, id ASC;", "ok"),
        ("limit", "SELECT id FROM a2 ORDER BY id LIMIT 1;", "ok"),
        ("limit offset", "SELECT id FROM a2 ORDER BY id LIMIT 1 OFFSET 1;", "ok"),
    ]),
    ("R4 vector topk", [
        ("l2 distance", "SELECT id, L2_DISTANCE(v, '[0,0,0]') AS d FROM b ORDER BY d LIMIT 1;", "ok"),
        ("filter then topk", "SELECT id FROM b WHERE id > 0 ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 1;", "ok"),
    ]),
    ("R5 illegal vectors", [
        ("nan", "INSERT INTO b VALUES (1, '[1, 2, nan]');", "fail"),
        ("inf", "INSERT INTO b VALUES (1, '[1, 2, inf]');", "fail"),
        ("too few dims", "INSERT INTO b VALUES (1, '[1, 2]');", "fail"),
        ("too many dims", "INSERT INTO b VALUES (1, '[1, 2, 3, 4]');", "fail"),
        ("float32 overflow", "INSERT INTO b VALUES (1, '[1, 2, 1e40]');", "fail"),
        ("non numeric", "INSERT INTO b VALUES (1, '[1, 2, x]');", "fail"),
    ]),
    ("R6 atomicity", [
        ("failed multi insert leaves nothing", "SELECT id FROM b;", ("rows", [])),
    ]),
    ("R11 vector index", [
        ("create vector index", "CREATE VECTOR INDEX b_vec ON b(v);", "ok"),
        ("create vector index twice", "CREATE VECTOR INDEX b_vec ON b(v);", "fail"),
        ("index on missing column", "CREATE VECTOR INDEX b_bad ON b(nope);", "fail"),
        ("drop index", "DROP INDEX b_vec;", "ok"),
    ]),
    ("R12 unsupported statements", [
        ("alter table", "ALTER TABLE a2 ADD x BIGINT;", "fail"),
        ("drop table", "DROP TABLE a2;", "ok"),
        ("aggregate count", "SELECT COUNT(*) FROM a;", "fail"),
        ("join", "SELECT * FROM a JOIN a2 ON a.id = a2.id;", "fail"),
        ("subquery", "SELECT id FROM a WHERE id IN (SELECT id FROM a2);", "fail"),
        ("null insert", "INSERT INTO a VALUES (NULL, 'x');", "fail"),
        ("foreign key", "CREATE TABLE f (id BIGINT PRIMARY KEY, r BIGINT REFERENCES a(id));", "fail"),
        ("update primary key", "UPDATE a SET id = 99 WHERE id = 1;", "fail"),
        ("nested transaction", "BEGIN;", "ok"),
        ("nested transaction rejected", "BEGIN;", "fail"),
        ("rollback the open transaction", "ROLLBACK;", "ok"),
    ]),
    ("R13 unicode", [
        ("unicode roundtrip", "CREATE TABLE u (id BIGINT PRIMARY KEY, c VARCHAR(16));", "ok"),
        ("insert unicode", "INSERT INTO u VALUES (1, '中文abc');", "ok"),
        ("select unicode", "SELECT c FROM u;", ("rows", [["中文abc"]])),
        ("varchar over length rejected", "INSERT INTO u VALUES (2, '0123456789abcdefghijk');", "fail"),
        ("unicode order", "CREATE TABLE u2 (id BIGINT PRIMARY KEY, c VARCHAR(16));", "ok"),
        ("insert for order", "INSERT INTO u2 VALUES (1, 'b'), (2, 'a'), (3, '中');", "ok"),
        ("unicode ordering", "SELECT c FROM u2 ORDER BY c;", ("rows", [["a"], ["b"], ["中"]])),
        ("varchar boundary by characters", "INSERT INTO u VALUES (3, '%s');" % ("中" * 16), "ok"),
        ("varchar boundary read back", "SELECT c FROM u WHERE id = 3;", None),
        ("varchar one over the boundary", "INSERT INTO u VALUES (4, '%s');" % ("中" * 17), "fail"),
        ("varchar ascii boundary", "INSERT INTO u VALUES (5, '%s');" % ("x" * 16), "ok"),
        ("varchar ascii one over", "INSERT INTO u VALUES (6, '%s');" % ("x" * 17), "fail"),
    ]),
]


def main():
    work = tempfile.mkdtemp(prefix="mvdb-probe-")
    db = Db(work, os.path.join(work, "stderr.log"))
    failures = []
    total = 0
    try:
        for group, items in GROUPS:
            print("\n== %s ==" % group, flush=True)
            for label, sql, expect in items:
                total += 1
                r = db.exec(sql)
                ok = r.get("ok") is True
                problem = None
                if expect == "ok" and not ok:
                    problem = "expected success, got %r" % (r.get("error"),)
                elif expect == "fail" and ok:
                    problem = "expected failure, got success"
                elif isinstance(expect, tuple) and expect[0] == "rows" and ok:
                    if r.get("rows") != expect[1]:
                        problem = "rows %r != %r" % (r.get("rows"), expect[1])
                elif isinstance(expect, tuple) and expect[0] == "columns" and ok:
                    if r.get("columns") != expect[1]:
                        problem = "columns %r != %r" % (r.get("columns"), expect[1])
                code = (r.get("error") or {}).get("code") if not ok else ""
                if problem:
                    failures.append((group, label, sql, problem))
                    print("  FAIL  %-34s %s" % (label, problem), flush=True)
                else:
                    print("  ok    %-34s %s" % (label, ("code=%s" % code) if code else ""), flush=True)
    finally:
        try:
            db.kill()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    print("\n==== %d statements, %d problems ====" % (total, len(failures)))
    for group, label, sql, problem in failures:
        print("  [%s] %s\n     sql: %s\n     why: %s" % (group, label, sql, problem))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
