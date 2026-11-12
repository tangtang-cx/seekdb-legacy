"""JSONL protocol server.

Contract (see STDIO_PROTOCOL.md of the task package):
  * one JSON request per line on stdin, one JSON response per line on stdout
  * stdout carries protocol messages only; every log goes to stderr
  * ``{"type":"ready"}`` is emitted proactively, before the first request
  * ``id`` is echoed verbatim (same JSON type), ``ok`` is a JSON boolean
  * after ``shutdown`` nothing else may be written and the exit code is 0
"""
from __future__ import annotations

import json
import os
import sys

from .core import INTERNAL_ERROR, SqlError, UNSUPPORTED_OP
from .engine import Engine


def _write(stream, payload: dict) -> None:
    try:
        line = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":"))
    except ValueError:
        # A non-finite number would make the response invalid JSON.  The
        # protocol forbids NaN and Infinity, so report the problem instead of
        # breaking the one-response-per-request contract.
        line = json.dumps({"id": payload.get("id"), "ok": False,
                           "error": {"code": INTERNAL_ERROR,
                                     "message": "response contains a non-finite number"}},
                          ensure_ascii=True, separators=(",", ":"))
    stream.write(line + "\n")
    stream.flush()


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def serve(data_dir: str) -> int:
    engine = Engine(data_dir)
    engine.open()

    out = sys.stdout
    _write(out, {"type": "ready"})

    stdin = sys.stdin.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            _log(f"invalid request line: {exc}")
            continue
        if not isinstance(request, dict):
            _log("request must be a JSON object")
            continue

        identifier = request.get("id")
        operation = request.get("op")

        if operation == "execute":
            sql = request.get("sql", "")
            mode = request.get("search_mode", "exact")
            try:
                result = engine.execute(sql, mode)
                response = {"id": identifier, "ok": True}
                response.update(result)
            except SqlError as exc:
                response = {"id": identifier, "ok": False,
                            "error": {"code": exc.code, "message": exc.message}}
            except RecursionError:
                response = {"id": identifier, "ok": False,
                            "error": {"code": "internal_error", "message": "expression too deep"}}
            except Exception as exc:                     # never kill the process
                _log(f"internal error: {exc!r}")
                response = {"id": identifier, "ok": False,
                            "error": {"code": INTERNAL_ERROR, "message": str(exc)}}
            _write(out, response)
            continue

        if operation == "shutdown":
            try:
                engine.close()
            except Exception as exc:
                _log(f"error during shutdown: {exc!r}")
                return 1
            _write(out, {"id": identifier, "ok": True})
            return 0

        _write(out, {"id": identifier, "ok": False,
                     "error": {"code": UNSUPPORTED_OP,
                               "message": f"unsupported op {operation!r}"}})
    # stdin closed without an explicit shutdown: behave like a shutdown.
    engine.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = os.environ.get("VECTORDB_DATA_DIR")
    if not data_dir:
        for index, argument in enumerate(argv):
            if argument in ("--data-dir", "-d") and index + 1 < len(argv):
                data_dir = argv[index + 1]
            elif argument.startswith("--data-dir="):
                data_dir = argument.split("=", 1)[1]
    if not data_dir:
        data_dir = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "data")
    _log(f"minivectordb starting, data_dir={data_dir}")
    # The vector fast path needs numpy.  If the image was built without it the
    # program still answers every request correctly, but a TopK scan costs
    # ~1250 ms instead of ~5 ms, so say which path this process will take.
    try:
        import numpy  # noqa: F401
        _log(f"numpy {numpy.__version__}: vector fast path enabled")
    except Exception as exc:  # noqa: BLE001 - the fallback is a valid mode
        _log(f"numpy unavailable ({exc!r}): pure standard library path")
    return serve(data_dir)


if __name__ == "__main__":
    sys.exit(main())
