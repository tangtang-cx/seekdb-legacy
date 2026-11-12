"""Crash durability test: a committed transaction survives a forced kill.

The task states that a confirmed commit has to survive the forced
termination of the process, that an open transaction must not take effect
partially, and that the recovery has to work when the process is killed
again while it recovers.

Usage:
    python tools/mvdb_crash_test.py [--rounds 4] [--rows 50]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, WS)

MAIN = os.path.join(WS, "main.py")
PYTHON = sys.executable


class Db:
    """One database process speaking the judge JSONL protocol."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.sequence = 0
        self.proc = subprocess.Popen(
            [PYTHON, "-u", MAIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, VECTORDB_DATA_DIR=data_dir))
        ready = self._readline()
        if ready != {"type": "ready"}:
            raise RuntimeError("expected ready, got %r" % (ready,))

    def _readline(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("database exited before response")
        return json.loads(line.decode("utf-8"))

    def exec(self, sql):
        self.sequence += 1
        request = {"id": self.sequence, "op": "execute", "sql": sql,
                   "search_mode": "exact"}
        self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        response = self._readline()
        if not response.get("ok"):
            raise RuntimeError("statement failed: %r" % (response,))
        return response

    def kill(self):
        """Terminate the process the way the judge does, without a shutdown."""

        self.proc.kill()
        self.proc.wait(timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--rows", type=int, default=50)
    args = parser.parse_args(argv)

    root = tempfile.mkdtemp(prefix="mvdb-crash-")
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    problems = 0
    try:
        db = Db(data_dir)
        db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));")
        for index in range(1, args.rows + 1):
            db.exec("INSERT INTO t VALUES (%d,'[%d,0]');" % (index, index))
        # An open transaction that must not take effect at all.
        db.exec("BEGIN;")
        db.exec("INSERT INTO t VALUES (%d,'[999,0]');" % (args.rows + 1))
        db.kill()

        for round_number in range(args.rounds):
            db = Db(data_dir)
            rows = db.exec("SELECT id FROM t ORDER BY id;")["rows"]
            ids = [row[0] for row in rows]
            # Every round appends one committed row before the next kill, so
            # the committed set grows by one per round.
            expected = list(range(1, args.rows + 1 + round_number))
            if ids != expected:
                problems += 1
                print("round %d: expected %d committed rows, got %d"
                      % (round_number, len(expected), len(ids)))
            else:
                print("round %d: %d committed rows survived the kill" %
                      (round_number, len(ids)))
            # The recovery has to leave a database that accepts new rows.
            db.exec("INSERT INTO t VALUES (%d,'[%d,0]');"
                    % (args.rows + 1 + round_number, args.rows + 1 + round_number))
            db.kill()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if problems:
        print("crash persistence FAILED (%d problems)" % problems)
        return 1
    print("crash persistence OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
