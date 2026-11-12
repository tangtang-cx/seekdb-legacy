"""Judge-equivalent local harness for MiniVectorDB.

The official driver (`protocol_check/run.py`) cannot run on Windows: its
transport puts the child's stdin pipe into non-blocking mode, which makes the
child fail its very first stdout write.  The judge runs Linux, where that
works.  This harness reproduces the same semantics (ready handshake,
request/response, `--restart`, `--mode`, the official assertion rules) in a
portable way and adds the scenarios the public package does not cover:

  * forced termination (SIGKILL) after a commit
  * forced termination with an open transaction
  * repeated recovery (kill during recovery)
  * data volume larger than the memory budget
  * ANN recall / filter / duplicate-id / validity rules
  * rejection of NaN, Infinity, wrong dimension and float32 overflow
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
PKG = os.environ.get("MVDB_PKG") or os.path.join(WS, "seekdb-round2")
MAIN = os.path.join(PKG, "main.py")
PYTHON = sys.executable

sys.path.insert(0, os.path.join(PKG, "protocol_check"))
from _assertions import assert_response  # noqa: E402
from _transport import strict_json       # noqa: E402


class Db:
    """One database process speaking the judge JSONL protocol."""

    def __init__(self, data_dir, log_path=None):
        self.data_dir = data_dir
        self.log = open(log_path, "ab", buffering=0) if log_path else subprocess.DEVNULL
        self.seq = 0
        env = dict(os.environ, VECTORDB_DATA_DIR=data_dir)
        self.proc = subprocess.Popen(
            [PYTHON, "-u", MAIN],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, env=env)
        ready = self._readline()
        if ready != {"type": "ready"}:
            raise RuntimeError("expected ready, got %r" % (ready,))

    def _readline(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("database exited before response")
        return strict_json(line.decode("utf-8"))

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
        if response.get("ok") is not True:
            raise RuntimeError("shutdown failed: %r" % (response,))
        self.proc.stdin.close()
        code = self.proc.wait(timeout=30)
        if code != 0:
            raise RuntimeError("nonzero exit after shutdown: %s" % code)
        return response

    def kill(self):
        """Terminate the process the way the judge kills a container."""
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        if self.log is not subprocess.DEVNULL:
            self.log.close()

    def close(self):
        try:
            self.kill()
        except Exception:
            pass


def count_rows(db, table):
    """Row count via a projection (aggregates are deliberately unsupported)."""
    r = db.exec("SELECT id FROM %s;" % table)
    if r.get("ok") is not True:
        raise AssertionError("count query failed: %r" % (r,))
    return len(r.get("rows") or [])


def load_cases():
    manifest = strict_json(open(os.path.join(PKG, "mysqltest", "manifest.json"),
                                encoding="utf-8").read())
    out = []
    for case in manifest["cases"]:
        base = os.path.join(PKG, "mysqltest")
        expectations = strict_json(open(os.path.join(base, case["result"]),
                                        encoding="utf-8").read())
        statements = open(os.path.join(base, case["test"]), encoding="utf-8").read().splitlines()
        out.append((case["id"], statements, expectations))
    return out


CONFIG = {"abs_tol": 1e-5, "rel_tol": 1e-5, "error_codes": {}}


def run_public_cases(verbose=False):
    """Replay every public case with the official assertion rules."""
    results = []
    for case_id, statements, expectations in load_cases():
        work = tempfile.mkdtemp(prefix="mvdb-case-")
        log = os.path.join(work, "stderr.log")
        db = Db(work, log)
        index, mode = 0, "exact"
        status, message = "PASS", ""
        try:
            for number, text in enumerate(statements, 1):
                sql = text.strip()
                if not sql or sql.startswith("#"):
                    continue
                if sql == "--restart":
                    db.shutdown()
                    db = Db(work, log)
                elif sql in ("--mode exact", "--mode ann"):
                    mode = sql.split()[1]
                elif sql.startswith("--"):
                    raise ValueError("unsupported directive: " + sql)
                else:
                    response = db.exec(sql, mode)
                    try:
                        assert_response(response, expectations[index], CONFIG)
                    except AssertionError as exc:
                        raise AssertionError("line %d: %s: %s" % (number, sql, exc)) from exc
                    index += 1
            if index != len(expectations):
                raise ValueError("SQL/result count mismatch")
        except Exception as exc:
            status, message = "FAIL", repr(exc)
        finally:
            try:
                db.kill()
            except Exception:
                pass
            shutil.rmtree(work, ignore_errors=True)
        results.append((case_id, status, message))
        print("  %-32s %s %s" % (case_id, status, message if status != "PASS" else ""), flush=True)
    return results


# ---------------------------------------------------------------- scenarios

def scenario_crash_durability():
    """Committed data survives a forced termination."""
    work = tempfile.mkdtemp(prefix="mvdb-crash-")
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(16));")
        for i in range(1, 51):
            r = db.exec("INSERT INTO t VALUES (%d, 'row%d');" % (i, i))
            if r.get("ok") is not True:
                raise AssertionError("insert failed: %r" % (r,))
        db.kill()

        db = Db(work, os.path.join(work, "stderr2.log"))
        got = count_rows(db, "t")
        if got != 50:
            raise AssertionError("expected 50 rows after crash, got %r" % (got,))
        db.shutdown()
        print("  crash_durability: PASS (50 rows survived SIGKILL)", flush=True)
        return True
    except Exception as exc:
        print("  crash_durability: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scenario_crash_uncommitted():
    """An open transaction leaves no partial effect after a forced termination."""
    work = tempfile.mkdtemp(prefix="mvdb-crash2-")
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(16));")
        db.exec("INSERT INTO t VALUES (1, 'keep');")
        db.exec("BEGIN;")
        db.exec("INSERT INTO t VALUES (2, 'lost');")
        db.exec("UPDATE t SET c = 'changed' WHERE id = 1;")
        db.kill()

        db = Db(work, os.path.join(work, "stderr2.log"))
        r = db.exec("SELECT id, c FROM t ORDER BY id;")
        rows = r.get("rows")
        if rows != [[1, "keep"]]:
            raise AssertionError("expected only the committed row, got %r" % (rows,))
        db.shutdown()
        print("  crash_uncommitted: PASS (no partial effect)", flush=True)
        return True
    except Exception as exc:
        print("  crash_uncommitted: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scenario_repeated_recovery():
    """Killing the process during recovery must not destroy the data."""
    work = tempfile.mkdtemp(prefix="mvdb-crash3-")
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, c VARCHAR(16));")
        for i in range(1, 201):
            db.exec("INSERT INTO t VALUES (%d, 'row%d');" % (i, i))
        db.kill()

        # First restart is killed immediately, so recovery is interrupted.
        db = Db(work, os.path.join(work, "stderr2.log"))
        db.kill()
        # Second restart must still recover everything.
        db = Db(work, os.path.join(work, "stderr3.log"))
        got = count_rows(db, "t")
        if got != 200:
            raise AssertionError("expected 200 rows after repeated recovery, got %r" % (got,))
        db.shutdown()
        print("  repeated_recovery: PASS (200 rows after interrupted recovery)", flush=True)
        return True
    except Exception as exc:
        print("  repeated_recovery: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scenario_illegal_vectors():
    """NaN, Infinity, wrong dimension and float32 overflow must be rejected."""
    work = tempfile.mkdtemp(prefix="mvdb-vec-")
    cases = [
        ("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(3));", None),
        ("INSERT INTO t VALUES (1, '[1, 2, nan]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2, inf]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2, -inf]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2, 3, 4]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2, 1e40]');", "must fail"),
        ("INSERT INTO t VALUES (1, '[1, 2, 3]');", None),
    ]
    ok = True
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        for sql, expect_fail in cases:
            r = db.exec(sql)
            if expect_fail and r.get("ok") is not False:
                print("    should have failed: %s -> %r" % (sql, r), flush=True)
                ok = False
            if expect_fail is None and r.get("ok") is not True:
                print("    should have succeeded: %s -> %r" % (sql, r), flush=True)
                ok = False
        leaked = count_rows(db, "t")
        if leaked != 1:
            print("    rejected rows leaked into the table: %r" % (leaked,), flush=True)
            ok = False
        db.shutdown()
        print("  illegal_vectors: %s" % ("PASS" if ok else "FAIL",), flush=True)
        return ok
    except Exception as exc:
        print("  illegal_vectors: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scenario_bulk_and_ann():
    """Bulk load, filtered TopK and ANN recall on a data set."""
    work = tempfile.mkdtemp(prefix="mvdb-bulk-")
    dim = 128
    rows = 10000
    random.seed(7)
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, grp BIGINT, v VECTOR(%d));" % dim)
        vectors = []
        batch = 200
        for start in range(0, rows, batch):
            values = []
            for i in range(start, min(start + batch, rows)):
                vec = [random.random() for _ in range(dim)]
                vectors.append(vec)
                values.append("(%d, %d, '%s')" % (
                    i + 1, (i + 1) % 10,
                    json.dumps(vec, separators=(",", ":"))))
            r = db.exec("INSERT INTO t VALUES %s;" % ", ".join(values))
            if r.get("ok") is not True:
                raise AssertionError("bulk insert failed: %r" % (r,))
        db.shutdown()

        db = Db(work, os.path.join(work, "stderr2.log"))
        got = count_rows(db, "t")
        if got != rows:
            raise AssertionError("row count wrong: %r" % (got,))

        queries = 20
        hits = 0
        for q in range(queries):
            probe = [random.random() for _ in range(dim)]
            # Ground truth over the filtered subset.
            truth = sorted(
                ((sum((a - b) ** 2 for a, b in zip(vectors[i], probe)) ** 0.5, i + 1)
                 for i in range(rows) if (i + 1) % 10 == q % 10))[:10]
            truth_ids = sorted(i for _, i in truth)
            sql = ("SELECT id, L2_DISTANCE(v, '%s') AS d FROM t WHERE grp = %d "
                   "ORDER BY d ASC, id ASC LIMIT 10;" % (
                       json.dumps(probe, separators=(",", ":")), q % 10))
            for mode in ("exact", "ann"):
                r = db.exec(sql, mode)
                if r.get("ok") is not True:
                    raise AssertionError("query failed (%s): %r" % (mode, r))
                got = [row[0] for row in (r.get("rows") or [])]
                if len(got) != len(set(got)):
                    raise AssertionError("duplicate ids in %s result: %r" % (mode, got))
                if not set(got) <= set(range(1, rows + 1)):
                    raise AssertionError("invalid ids in %s result" % mode)
                if mode == "exact":
                    if sorted(got) != truth_ids:
                        raise AssertionError("exact TopK mismatch: %r != %r"
                                             % (sorted(got), truth_ids))
                else:
                    overlap = len(set(got) & set(truth_ids))
                    hits += overlap
        recall = hits / (queries * 10.0)
        if recall < 0.95:
            raise AssertionError("ANN recall %.4f below 0.95" % recall)
        db.shutdown()
        print("  bulk_and_ann: PASS (rows=%d, ann recall=%.3f)" % (rows, recall), flush=True)
        return True
    except Exception as exc:
        print("  bulk_and_ann: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scenario_memory_pressure():
    """A data set larger than the memory budget must still work."""
    work = tempfile.mkdtemp(prefix="mvdb-mem-")
    try:
        db = Db(work, os.path.join(work, "stderr.log"))
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(64));")
        batch = 500
        total = 20000
        for start in range(0, total, batch):
            values = []
            for i in range(start, min(start + batch, total)):
                vec = [float(i % 97) + j * 0.5 for j in range(64)]
                values.append("(%d, '%s')" % (i + 1, json.dumps(vec, separators=(",", ":"))))
            r = db.exec("INSERT INTO t VALUES %s;" % ", ".join(values))
            if r.get("ok") is not True:
                raise AssertionError("bulk insert failed: %r" % (r,))
        got = count_rows(db, "t")
        if got != total:
            raise AssertionError("row count wrong: %r" % (got,))
        r = db.exec("SELECT id FROM t WHERE id = 12345;")
        if (r.get("rows") or []) != [[12345]]:
            raise AssertionError("point lookup failed: %r" % (r.get("rows"),))
        db.shutdown()
        print("  memory_pressure: PASS (%d rows, multi-row insert)" % total, flush=True)
        return True
    except Exception as exc:
        print("  memory_pressure: FAIL %r" % (exc,), flush=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    import importlib
    numpy_ok = False
    try:
        import numpy
        numpy_ok = True
        version = numpy.__version__
    except Exception:
        version = "absent"
    print("python=%s numpy=%s" % (sys.version.split()[0], version), flush=True)

    print("\n== public cases ==", flush=True)
    public = run_public_cases()
    public_ok = all(status == "PASS" for _, status, _ in public)

    print("\n== judge scenarios ==", flush=True)
    results = [
        scenario_crash_durability(),
        scenario_crash_uncommitted(),
        scenario_repeated_recovery(),
        scenario_illegal_vectors(),
        scenario_memory_pressure(),
        scenario_bulk_and_ann(),
    ]
    print("\nSUMMARY public=%s scenarios=%s" %
          ("PASS" if public_ok else "FAIL",
           "PASS" if all(results) else "FAIL"))
    return 0 if (public_ok and all(results)) else 1


if __name__ == "__main__":
    sys.exit(main())
