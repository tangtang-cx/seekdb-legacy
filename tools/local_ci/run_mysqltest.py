#!/usr/bin/env python3
"""A small mysqltest-compatible driver for the seekdb competition test suites.

The upstream Perl ``mysqltest`` driver is not part of the seekdb repository, so
this script re-implements the subset of the ``.test`` / ``.result`` dialect the
competition suites use:

* ``--echo``, ``--error``, ``--disable_result_log`` / ``--enable_result_log``
* ``--disable_warnings`` / ``--enable_warnings``
* ``--source <file>`` (relative to the mysql_test root)
* ``let $VAR = ...`` and ``eval ...``
* plain SQL statements, possibly spanning several lines

Statements are sent to a running observer over the MySQL protocol with
``pymysql``.  The produced transcript is compared against the ``.result`` file
that ships with the suite, using the same "ignore trailing horizontal
whitespace" rule as the upstream runner.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MYSQL_TEST_ROOT = REPO_ROOT / "tools" / "deploy" / "mysql_test"
SUITE_ROOT = MYSQL_TEST_ROOT / "test_suite"
OUT_ROOT = Path(os.environ.get("SEEKDB_LOCAL_CI_OUT", "/tmp/seekdb_local_ci"))


class CaseError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# .test parsing
# --------------------------------------------------------------------------
class Statement:
    def __init__(self, text, line_no, expect_errors=None, result_log=True, echo=None):
        self.text = text
        self.line_no = line_no
        self.expect_errors = expect_errors
        self.result_log = result_log
        self.echo = echo


def expand(text, variables):
    def repl(match):
        name = match.group(1)
        if name not in variables:
            raise CaseError("undefined variable $%s" % name)
        return variables[name]

    return re.sub(r"\$(\w+)", repl, text)


def parse_test(path, variables, source_stack=None):
    """Return the list of statements and the echo lines for one .test file."""
    actions = []
    expect_errors = None
    result_log = True
    pending = []
    pending_start = 0
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        line_no = index + 1
        index += 1

        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("--"):
            directive = stripped[2:].strip()
            if directive.startswith("echo "):
                actions.append(("echo", directive[5:]))
            elif directive.startswith("error "):
                codes = []
                for item in directive[6:].split(","):
                    item = item.strip()
                    if not item:
                        continue
                    try:
                        codes.append(int(item))
                    except ValueError:
                        pass
                expect_errors = codes or [0]
            elif directive in ("disable_result_log", "enable_result_log"):
                result_log = directive == "enable_result_log"
            elif directive in ("disable_warnings", "enable_warnings"):
                pass
            elif directive.startswith("source "):
                target = directive[7:].strip()
                source_path = (MYSQL_TEST_ROOT / target).resolve()
                if not str(source_path).startswith(str(MYSQL_TEST_ROOT.resolve())):
                    raise CaseError("source escapes the mysql_test root: %s" % target)
                if not source_path.is_file():
                    raise CaseError("missing source file: %s" % target)
                sub = parse_test(source_path, variables)
                actions.extend(sub)
            elif directive.startswith("let ") or directive.startswith("Let "):
                body = directive[4:].strip()
                if "=" not in body:
                    raise CaseError("unsupported let directive: %s" % directive)
                name, value = body.split("=", 1)
                variables[name.strip().lstrip("$")] = value.strip().strip("'\"")
            elif directive.startswith("replace_column") or directive.startswith("sorted_result"):
                raise CaseError("unsupported directive: %s" % directive)
            else:
                # Unknown directives are ignored on purpose: they only affect
                # formatting of the reference transcript.
                pass
            continue

        if stripped.lower().startswith("connection "):
            continue
        if stripped.lower().startswith("let "):
            body = stripped[4:].strip()
            if "=" in body:
                name, value = body.split("=", 1)
                variables[name.strip().lstrip("$")] = value.strip().strip("'\"")
            continue

        if not pending:
            pending_start = line_no
        pending.append(raw)
        joined = "\n".join(pending)
        if not joined.rstrip().endswith(";"):
            continue

        sql = joined.rstrip()
        pending = []
        if stripped.lower().startswith("eval "):
            sql = expand(sql[5:].strip(), variables)
        actions.append(
            (
                "sql",
                Statement(
                    sql,
                    pending_start,
                    expect_errors=expect_errors,
                    result_log=result_log,
                ),
            )
        )
        expect_errors = None
    if pending:
        raise CaseError("unterminated statement near line %d" % pending_start)
    return actions


# --------------------------------------------------------------------------
# result formatting
# --------------------------------------------------------------------------
def format_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def render(statement, columns, rows, error=None):
    out = []
    if statement.result_log:
        out.append(statement.text)
        if error is None and columns:
            out.append("\t".join(str(c) for c in columns))
            for row in rows:
                out.append("\t".join(format_value(v) for v in row))
    return out


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def connect(host, port, user, password):
    import pymysql

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.Cursor,
    )


def run_case(case_id, test_path, result_path, host, port, user, password):
    transcript = []
    variables = {}
    actions = parse_test(test_path, variables)
    connection = connect(host, port, user, password)
    try:
        with connection.cursor() as cursor:
            for kind, payload in actions:
                if kind == "echo":
                    transcript.append(payload)
                    continue
                statement = payload
                columns = []
                rows = []
                error = None
                try:
                    cursor.execute(statement.text)
                    if cursor.description:
                        columns = [d[0] for d in cursor.description]
                        rows = list(cursor.fetchall())
                except Exception as exc:  # noqa: BLE001
                    error = exc
                if statement.expect_errors is not None:
                    if error is None:
                        if 0 not in statement.expect_errors:
                            raise CaseError(
                                "line %d: expected error %s but the statement succeeded"
                                % (statement.line_no, statement.expect_errors)
                            )
                    else:
                        code = getattr(error, "args", [None])[0]
                        if not isinstance(code, int):
                            code = 0
                        if code not in statement.expect_errors:
                            raise CaseError(
                                "line %d: expected error %s, got %s (%s)"
                                % (statement.line_no, statement.expect_errors, code, error)
                            )
                elif error is not None:
                    raise CaseError(
                        "line %d: unexpected error: %s"
                        % (statement.line_no, error)
                    )
                transcript.extend(render(statement, columns, rows, error))
    finally:
        connection.close()
    return transcript


def normalize(lines):
    return [line.rstrip(" \t") for line in lines]


def compare(expected_path, actual_lines):
    expected = expected_path.read_text(encoding="utf-8").splitlines()
    expected = [line.rstrip(" \t") for line in expected]
    actual = normalize(actual_lines)
    if expected == actual:
        return None
    diff = difflib.unified_diff(
        expected, actual, fromfile="expected", tofile="actual", lineterm=""
    )
    return "\n".join(diff)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="ai_function")
    parser.add_argument("--case", action="append", help="run only this case id")
    parser.add_argument("--host", default=os.environ.get("SEEKDB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEEKDB_PORT", "2881")))
    parser.add_argument("--user", default=os.environ.get("SEEKDB_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("SEEKDB_PASSWORD", ""))
    args = parser.parse_args(argv)

    suite_dir = SUITE_ROOT / args.suite
    test_dir = suite_dir / "t"
    result_dir = suite_dir / "r" / "mysql"
    if not test_dir.is_dir():
        raise SystemExit("unknown suite: %s" % args.suite)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    for test_path in sorted(test_dir.glob("*.test")):
        case_id = test_path.stem
        if args.case and case_id not in args.case:
            continue
        result_path = result_dir / (case_id + ".result")
        if not result_path.is_file():
            results.append((case_id, "SKIP", "no result file"))
            print("%s: SKIP (no result file)" % case_id, flush=True)
            continue
        started = time.monotonic()
        try:
            transcript = run_case(
                case_id,
                test_path,
                result_path,
                args.host,
                args.port,
                args.user,
                args.password,
            )
        except Exception as exc:  # noqa: BLE001
            detail = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            (OUT_ROOT / (case_id + ".error.log")).write_text(detail, encoding="utf-8")
            results.append((case_id, "FAIL", str(exc)))
            failures.append(case_id)
            print("%s: FAIL %s" % (case_id, exc), flush=True)
            continue
        diff = compare(result_path, transcript)
        elapsed = time.monotonic() - started
        if diff is None:
            results.append((case_id, "PASS", ""))
            print("%s: PASS (%.1fs)" % (case_id, elapsed), flush=True)
        else:
            (OUT_ROOT / (case_id + ".diff.txt")).write_text(diff, encoding="utf-8")
            (OUT_ROOT / (case_id + ".actual.txt")).write_text(
                "\n".join(transcript) + "\n", encoding="utf-8"
            )
            results.append((case_id, "FAIL", "result mismatch"))
            failures.append(case_id)
            print("%s: FAIL result mismatch" % case_id, flush=True)

    passed = not failures
    summary = "\n".join("%s %s %s" % item for item in results)
    (OUT_ROOT / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n%s" % summary)
    print("overall: %s" % ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
