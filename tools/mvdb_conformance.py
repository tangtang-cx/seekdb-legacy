"""Conformance runner: every local check of MiniVectorDB in one command.

The task package says that the public cases do not cover the whole
admission requirement, so the local suites add the hidden requirements as
executable assertions: the illegal vectors, the atomicity of a failed
statement, the crash durability, the repeated recovery, the data volume,
the ANN recall and the agreement between the vectorised path and the
generic executor.

Usage:
    python tools/mvdb_conformance.py [--quick]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)

SUITES = [
    ("public cases and judge scenarios", "mvdb_judge_sim.py",
     [r"public=(?P<ok>\w+) scenarios=(?P<ok2>\w+)"],
     lambda ok, ok2: 20 + 6 if ok == "PASS" and ok2 == "PASS" else 0,
     lambda ok, ok2: 0 if ok == "PASS" and ok2 == "PASS" else 1),
    ("sql admission probe", "mvdb_sql_probe.py",
     [r"(?P<n>\d+) statements, (?P<bad>\d+) problems"],
     lambda n, bad: int(n),
     lambda n, bad: int(bad)),
    ("fast path semantics", "mvdb_semantics.py",
     [r"(?P<ok>all semantics checks passed)"],
     lambda ok: 21 if ok else 0,
     lambda ok: 0 if ok else 1),
    ("fast path against the generic executor", "mvdb_differential.py",
     [r"(?P<n>\d+) statements compared, (?P<bad>\d+) differences"],
     lambda n, bad: int(n),
     lambda n, bad: int(bad)),
    ("random differential queries", "mvdb_fuzz.py",
     [r"queries=(?P<n>\d+) mismatches=(?P<bad>\d+)"],
     lambda n, bad: int(n),
     lambda n, bad: int(bad)),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="skip the slowest suite")
    args = parser.parse_args(argv)

    environment = dict(os.environ, MVDB_PKG=WS)
    checks = 0
    failures = 0
    for name, script, patterns, count_of, failures_of in SUITES:
        if args.quick and script == "mvdb_fuzz.py":
            continue
        command = [sys.executable, os.path.join(HERE, script)]
        finished = subprocess.run(command, capture_output=True, text=True,
                                  env=environment, cwd=WS)
        output = finished.stdout
        matched = None
        for pattern in patterns:
            found = re.search(pattern, output)
            if found:
                matched = found
                break
        if matched is None:
            checks += 1
            failures += 1
            print("%-42s FAILED (no result line)" % name)
            print(output[-800:])
            print(finished.stderr[-800:])
            continue
        groups = matched.groupdict()
        suite_checks = count_of(*groups.values())
        suite_failures = failures_of(*groups.values())
        checks += suite_checks
        failures += suite_failures
        print("%-42s %s (%d checks, %d failures)"
              % (name, "PASS" if suite_failures == 0 else "FAIL",
                 suite_checks, suite_failures))

    print("checks=%d failures=%d" % (checks, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
