"""Apply the 2026-11-12 optimisation round to the MiniVectorDB sources.

The changes are the ones that were measured and committed during the round:
the vectorised path has to accept the quoted vector literal, the column
mirror has to follow an UPDATE, the file arena has to buffer its writes,
and the hot loops of the parser and of the filter have to be vectorised.

Every replacement is delimited by two marker lines, so the patch does not
depend on the blank lines of the original file.  The script is idempotent:
a change that is already present is skipped.

Usage:
    python tools/mvdb_apply_optimisations.py
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def apply(name, start, end, new):
    """Replace the lines between ``start`` and ``end`` with ``new``."""

    path = os.path.join(ROOT, "minivectordb", name)
    with io.open(path, encoding="utf-8", newline="") as handle:
        lines = handle.read().replace("\r\n", "\n").split("\n")
    if new.strip().split("\n")[0] in lines and start not in lines:
        print("  %s: already applied" % name)
        return
    try:
        first = lines.index(start)
        last = lines.index(end, first)
    except ValueError:
        raise SystemExit("%s: marker not found (%r / %r)" % (name, start, end))
    lines[first:last + 1] = new.split("\n")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    print("  %s: %s" % (name, start.strip()[:60]))


CORE_VECTOR = '''def parse_vector_literal(text: str) -> list[float]:
    """Parse '[0, 2]' / '[0.5,-1]' into floats.

    Raises SqlError(invalid_vector) for anything that is not a finite number.
    """

    body = text.strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    body = body.strip()
    if not body:
        raise SqlError(INVALID_VECTOR, "empty vector literal")
    fast = _parse_vector_fast(body)
    if fast is not None:
        return fast
    values: list[float] = []
    for token in body.split(","):
        token = token.strip()
        if not token:
            raise SqlError(INVALID_VECTOR, "empty element in vector literal")
        try:
            value = float(token)
        except ValueError:
            raise SqlError(INVALID_VECTOR, f"not a number: {token!r}") from None
        if not math.isfinite(value):
            raise SqlError(INVALID_VECTOR, "vector contains NaN or Infinity")
        values.append(value)
    return values


def _parse_vector_fast(body: str) -> list[float] | None:
    """Convert a plain list of numbers with the C float parser, or return ``None``.

    The strict loop above is the reference behaviour; this shortcut only
    handles the shape a generated workload uses (a comma separated list of
    decimal numbers).  Anything else falls back to the reference loop, so
    the accepted syntax and the raised errors stay exactly the same.
    """

    if _np is None:
        return None
    parts = body.split(",")
    if len(parts) < 8:
        return None
    try:
        values = list(map(float, parts))
    except ValueError:
        return None
    if len(values) != len(parts):
        return None
    if not bool(_np.isfinite(_np.asarray(values, dtype=_np.float64)).all()):
        raise SqlError(INVALID_VECTOR, "vector contains NaN or Infinity")
    return values'''

CORE_FLOAT32 = '''def to_float32(values: Sequence[float]) -> array.array:
    """Store vectors with float32 semantics, as the task requires.

    A value that cannot be represented as a finite float32 is rejected:
    NaN, Infinity and doubles outside the float32 range are all errors.
    """

    if _np is not None and len(values) >= 8:
        packed = _to_float32_fast(values)
        if packed is not None:
            return packed
    out = array.array("f")
    for value in values:
        value = float(value)
        if not math.isfinite(value):
            raise SqlError(INVALID_VECTOR, "vector contains NaN or Infinity")
        if value > FLOAT32_MAX or value < -FLOAT32_MAX:
            raise SqlError(VALUE_OUT_OF_RANGE, "vector element overflows float32")
        out.append(value)
        if not math.isfinite(out[-1]):
            raise SqlError(VALUE_OUT_OF_RANGE, "vector element overflows float32")
    if len(out) != len(values):
        raise SqlError(VALUE_OUT_OF_RANGE, "vector element overflows float32")
    return out


def _to_float32_fast(values: Sequence[float]) -> array.array | None:
    """Pack a vector into float32 with numpy, or return ``None``.

    The checks run in the same order as the reference loop: the first
    element that is either not finite or out of range decides the error, and
    a value that is not finite wins over a value that only overflows.
    """

    if _np is None:
        return None
    try:
        source = _np.asarray(values, dtype=_np.float64)
    except (TypeError, ValueError):
        return None
    if source.ndim != 1 or source.shape[0] != len(values):
        return None
    finite = _np.isfinite(source)
    over = _np.abs(source) > FLOAT32_MAX
    broken = _np.flatnonzero(~finite | over)
    if broken.size:
        index = int(broken[0])
        if not bool(finite[index]):
            raise SqlError(INVALID_VECTOR, "vector contains NaN or Infinity")
        raise SqlError(VALUE_OUT_OF_RANGE, "vector element overflows float32")
    packed = source.astype(_np.float32)
    if not bool(_np.isfinite(packed).all()):
        raise SqlError(VALUE_OUT_OF_RANGE, "vector element overflows float32")
    out = array.array("f")
    out.frombytes(packed.tobytes())
    return out


# The float32 view of a vector literal, memoised by its text.  A query
# evaluates its distance expression once per selected row, and every
# evaluation would otherwise parse the same literal again.
_LITERAL_CACHE: dict[str, array.array] = {}
_LITERAL_CACHE_LIMIT = 64


def to_float32_literal(text: str) -> array.array:
    """The float32 values of a vector literal, memoised by its text.

    The returned array is a copy, so the caller can never change the cached
    value.
    """

    cached = _LITERAL_CACHE.get(text)
    if cached is None:
        cached = to_float32(parse_vector_literal(text))
        if len(_LITERAL_CACHE) >= _LITERAL_CACHE_LIMIT:
            _LITERAL_CACHE.clear()
        _LITERAL_CACHE[text] = cached
    return array.array("f", cached)'''

DECODE_STRING = '''def _decode_string(raw: str) -> str:
    body = raw[1:-1]
    # The common case is a literal without escapes.  Unescaping it character
    # by character is the most expensive part of tokenising a bulk INSERT,
    # because every vector literal is a string of several hundred digits.
    if "\\\\" not in body and "'" not in body:
        return body
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\\\" and index + 1 < len(body):
            nxt = body[index + 1]
            mapping = {"n": "\\n", "t": "\\t", "r": "\\r", "0": "\\0", "\\\\": "\\\\",
                       "'": "'", '"': '"', "b": "\\b", "Z": "\\x1a"}
            out.append(mapping.get(nxt, nxt))
            index += 2
            continue
        if char == "'" and index + 1 < len(body) and body[index + 1] == "'":
            out.append("'")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)'''

SCAN_STRING = '''def _scan_string(sql: str, start: int) -> int:
    """The offset just past the string literal that starts at ``start``.

    A quote ends the literal unless it is doubled or preceded by an odd
    number of backslashes, which is exactly what the token pattern accepts.
    """

    length = len(sql)
    pos = start + 1
    while True:
        pos = sql.find("'", pos)
        if pos < 0:
            raise SqlError(SYNTAX_ERROR, "unterminated string literal")
        backslashes = 0
        index = pos - 1
        while index > start and sql[index] == "\\\\":
            backslashes += 1
            index -= 1
        if backslashes % 2:
            pos += 1
            continue
        if sql[pos + 1:pos + 2] == "'":
            pos += 2
            continue
        return pos + 1'''

BYTEFILE = '''class ByteFile:
    """Append-only byte arena with random access and cheap truncation.

    Writes are collected in a buffer and reach the file in one syscall.  A
    bulk insert touches every column of every row, so a syscall per value
    costs far more than the data itself; the buffer turns twenty two writes
    per row into a handful of block writes.

    The buffer always holds the tail of the file, that is the bytes between
    ``_flushed`` (what is on disk) and ``_size`` (the logical size).  A read
    that reaches into the tail is served from the buffer, a read that stays
    below it is served from the memory mapping, and a read that spans the
    boundary is assembled from both.
    """

    __slots__ = ("path", "_fd", "_size", "_map", "_buffer", "_flushed")

    # The buffer is flushed once it grows past this size, so the memory one
    # file can hold stays bounded no matter how large the table becomes.
    FLUSH_BYTES = 1 << 20

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)
        self._size = os.fstat(self._fd).st_size
        self._map = None
        self._buffer = bytearray()
        self._flushed = self._size

    # -- writing -----------------------------------------------------------
    def append(self, data: bytes) -> int:
        offset = self._size
        self._buffer += data
        self._size += len(data)
        if len(self._buffer) >= self.FLUSH_BYTES:
            self._flush()
        return offset

    def write_at(self, offset: int, data: bytes) -> None:
        if not data:
            return
        end = offset + len(data)
        if offset >= self._flushed:
            # The write lands in the buffered tail.  A gap between the
            # current size and the offset is a hole of zero bytes, exactly
            # what a write past the end of the file would leave behind.
            if end > self._size:
                if offset > self._size:
                    self._buffer += bytes(offset - self._size)
                self._buffer += data
                self._size = end
            else:
                start = offset - self._flushed
                self._buffer[start:start + len(data)] = data
            if len(self._buffer) >= self.FLUSH_BYTES:
                self._flush()
            return
        # The write starts below the flushed region.  The part that is
        # already on disk goes straight to the file, the part that reaches
        # into the tail updates the buffer.
        boundary = min(end, self._flushed)
        head = memoryview(data)[:boundary - offset]
        os.lseek(self._fd, offset, os.SEEK_SET)
        while head:
            head = head[os.write(self._fd, head):]
        if boundary < end:
            tail = data[boundary - offset:]
            start = boundary - self._flushed
            self._buffer[start:start + len(tail)] = tail
        if end > self._size:
            self._size = end

    def _flush(self) -> None:
        """Write the buffered tail to the file."""

        if not self._buffer:
            return
        os.lseek(self._fd, self._flushed, os.SEEK_SET)
        view = memoryview(self._buffer)
        while view:
            view = view[os.write(self._fd, view):]
        self._flushed += len(self._buffer)
        self._buffer = bytearray()

    def flush(self) -> None:
        """Hand the buffered tail to the operating system."""

        self._flush()

    def truncate(self, size: int) -> None:
        if size == self._size:
            self._flush()
            return
        self._flush()
        try:
            os.ftruncate(self._fd, size)
        except OSError:
            # Windows rejects ftruncate() on descriptors obtained through
            # os.open(); the path based call has the same effect.
            os.truncate(self.path, size)
        self._size = size
        self._flushed = size
        self._buffer = bytearray()
        if self._map is not None:
            try:
                if size == 0:
                    self._map.close()
                    self._map = None
                elif size <= len(self._map):
                    self._map.resize(size)
                else:
                    self._map.close()
                    self._map = None
            except (OSError, ValueError):
                try:
                    self._map.close()
                except (OSError, ValueError):
                    pass
                self._map = None

    # -- reading -----------------------------------------------------------
    def _ensure_map(self) -> None:
        """Refresh the mapping, but only when it is worth the remap."""

        size = self._flushed
        if size <= 0:
            return
        if self._map is None:
            if size >= MAP_MIN:
                try:
                    self._map = mmap.mmap(self._fd, size)
                except (OSError, ValueError):
                    self._map = None
            return
        mapped = len(self._map)
        if size > mapped and (size - mapped >= MAP_CHUNK or mapped < MAP_CHUNK):
            try:
                self._map.resize(size)
            except (OSError, ValueError):
                try:
                    self._map.close()
                except (OSError, ValueError):
                    pass
                self._map = None

    def read(self, offset: int, size: int) -> bytes:
        if size <= 0:
            return b""
        flushed = self._flushed
        if offset >= flushed:
            # The whole range is in the buffered tail.
            start = offset - flushed
            if start >= len(self._buffer):
                return b""
            return bytes(self._buffer[start:start + size])
        head = size
        if offset + size > flushed:
            head = flushed - offset
        data = self._read_file(offset, head)
        if head < size:
            data += bytes(self._buffer[:size - head])
        return data

    def _read_file(self, offset: int, size: int) -> bytes:
        """Read a range that is already on disk."""

        self._ensure_map()
        if self._map is not None and offset + size <= len(self._map):
            chunk = self._map[offset:offset + size]
            if len(chunk) == size:
                return bytes(chunk)
        os.lseek(self._fd, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = os.read(self._fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_all(self) -> bytes:
        return self.read(0, self._size)

    # -- lifecycle ---------------------------------------------------------
    def sync(self) -> None:
        self._flush()
        os.fsync(self._fd)

    def close(self) -> None:
        self._flush()
        if self._map is not None:
            try:
                self._map.close()
            except (OSError, ValueError):
                pass
            self._map = None
        try:
            os.close(self._fd)
        except OSError:
            pass

    @property
    def size(self) -> int:
        return self._size'''


def main():
    print("applying the optimisation round")

    # -- core.py ----------------------------------------------------------
    path = os.path.join(ROOT, "minivectordb", "core.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "import numpy as _np" not in text:
        text = text.replace(
            "# ---------------------------------------------------------------- vectors\n",
            "# ---------------------------------------------------------------- vectors\n"
            "\n"
            "try:  # pragma: no cover - depends on the image\n"
            "    import numpy as _np\n"
            "except ImportError:  # pragma: no cover - depends on the image\n"
            "    _np = None  # type: ignore[assignment]\n",
            1)
        print("  core.py: numpy import")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    apply("core.py", "def parse_vector_literal(text: str) -> list[float]:",
          "    return values", CORE_VECTOR)
    apply("core.py", "def to_float32(values: Sequence[float]) -> array.array:",
          "    return out", CORE_FLOAT32)

    # -- sqlparser.py -----------------------------------------------------
    apply("sqlparser.py", "def _decode_string(raw: str) -> str:",
          '    return "".join(out)', DECODE_STRING)
    apply("sqlparser.py", "def _vector_values(text: str) -> list[float]:",
          "def _vector_values(text: str) -> list[float]:", SCAN_STRING)
    path = os.path.join(ROOT, "minivectordb", "sqlparser.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "_scan_string(sql, pos)" not in text:
        text = text.replace(
            "        match = TOKEN_RE.match(sql, pos)\n"
            "        if not match:\n"
            "            raise SqlError(SYNTAX_ERROR, f\"unexpected character at {pos}: {sql[pos]!r}\")\n",
            "        if sql[pos] == \"'\":\n"
            "            # A quoted literal is scanned without the regular expression: a\n"
            "            # bulk insert carries one literal of a few hundred digits per\n"
            "            # row, and matching that with the token pattern costs more than\n"
            "            # the rest of the statement.\n"
            "            end = _scan_string(sql, pos)\n"
            "            tokens.append(Token(\"string\", _decode_string(sql[pos:end]), pos))\n"
            "            pos = end\n"
            "            continue\n"
            "        match = TOKEN_RE.match(sql, pos)\n"
            "        if not match:\n"
            "            raise SqlError(SYNTAX_ERROR, f\"unexpected character at {pos}: {sql[pos]!r}\")\n",
            1)
        text = text.replace(
            "        elif kind == \"string\":\n"
            "            tokens.append(Token(\"string\", _decode_string(text), match.start()))\n",
            "", 1)
        print("  sqlparser.py: the quoted literal is scanned without the pattern")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    # -- fastpath.py ------------------------------------------------------
    apply("fastpath.py", "def compare_bytes(arena: bytes, offsets: Any, lengths: Any, literal: bytes, operator: str) -> Any:",
          "    return mask", '''def compare_bytes(arena: bytes, offsets: Any, lengths: Any, literal: bytes, operator: str) -> Any:
    """Compare a VARCHAR column against a literal, byte by byte.

    UTF-8 byte order equals Unicode code point order, so the byte comparison
    gives the same ordering the reference executor uses for strings.  Only the
    slots whose byte length matches are decoded, which keeps the common
    equality filter cheap.

    When every slot holds a string of the same length and the strings are
    stored one after the other, the arena is read as an array of fixed width
    byte strings and the comparison runs in a single vectorised operation.
    The byte loop stays as the fallback for every other layout.
    """

    if _np is None:
        return None
    total = int(offsets.shape[0])
    mask = _np.zeros(total, dtype=bool)
    literal_length = len(literal)
    same_length = _np.flatnonzero(lengths == literal_length)
    if same_length.size == 0:
        return mask
    if literal_length and same_length.size == total:
        first = int(offsets[0])
        last = int(offsets[-1]) + literal_length
        if last <= len(arena):
            steps = _np.diff(offsets.astype(_np.int64))
            if bool(_np.all(steps == literal_length)):
                view = _np.frombuffer(arena, dtype="S%d" % literal_length,
                                      count=total, offset=first)
                if operator == "=":
                    return view == literal
                if operator in ("<>", "!="):
                    return view != literal
                if operator == "<":
                    return view < literal
                if operator == "<=":
                    return view <= literal
                if operator == ">":
                    return view > literal
                return view >= literal
    for slot in same_length:
        start = int(offsets[slot])
        end = start + literal_length
        value = arena[start:end]
        if operator == "=":
            mask[slot] = value == literal
        elif operator in ("<>", "!="):
            mask[slot] = value != literal
        elif operator == "<":
            mask[slot] = value < literal
        elif operator == "<=":
            mask[slot] = value <= literal
        elif operator == ">":
            mask[slot] = value > literal
        else:
            mask[slot] = value >= literal
    return mask''')
    path = os.path.join(ROOT, "minivectordb", "fastpath.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "tolerance = max(1e-9, abs(float(boundary)) * 1e-9)" not in text:
        text = text.replace(
            "        boundary = distances[partition].max()\n"
            "        candidates = _np.flatnonzero(distances <= boundary)\n",
            "        boundary = distances[partition].max()\n"
            "        # The tie is decided on a tolerance rather than on an exact\n"
            "        # comparison: the vectorised distance and the reference distance\n"
            "        # sum the same squares in a different order, so two rows whose\n"
            "        # reference distances are equal can end up a few ulp apart in the\n"
            "        # vectorised result.  The rows that survive are ordered by the\n"
            "        # reference distance again before the answer is returned.\n"
            "        tolerance = max(1e-9, abs(float(boundary)) * 1e-9)\n"
            "        candidates = _np.flatnonzero(distances <= boundary + tolerance)\n",
            1)
        print("  fastpath.py: the tie is decided on a tolerance")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    # -- store.py ---------------------------------------------------------
    path = os.path.join(ROOT, "minivectordb", "store.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "MAP_MIN" not in text:
        text = text.replace(
            "# mmap.resize() costs tens of microseconds, so the mapping is refreshed in\n"
            "# coarse chunks instead of on every write.\n"
            "MAP_CHUNK = 1 << 20\n",
            "# A file is mapped as soon as it holds a page of data, so the small columns\n"
            "# (the primary keys, the liveness flags, the offsets) are served from the\n"
            "# page cache instead of one read syscall per value.\n"
            "MAP_MIN = 4096\n"
            "\n"
            "# mmap.resize() costs tens of microseconds, so the mapping of a big file is\n"
            "# refreshed in coarse chunks instead of on every write.\n"
            "MAP_CHUNK = 1 << 20\n",
            1)
        print("  store.py: the mapping threshold")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    apply("store.py", "class ByteFile:", "    @property", BYTEFILE)
    apply("store.py", "    def delete(self, pk: int) -> None:",
          "    # -- durability --------------------------------------------------------",
          '''    def delete(self, pk: int) -> None:
        slot = self._index.get(pk)
        if slot is None:
            return
        self._live.write_at(slot, b"\\x00")
        self._index.remove(pk)

    def _refresh_float64(self, position: int, slot: int, payload: bytes) -> None:
        """Keep the cached float64 copy of a vector column in sync.

        ``vector_matrix64`` keeps a float64 copy of the column so the scan
        does not have to convert the column on every query.  An UPDATE does
        not change the size of the arena, so the copy would keep the old
        values and the scan would rank the rows with stale vectors.  The
        written row is therefore patched into the copy; when the copy cannot
        be patched it is dropped, and the next query rebuilds it.
        """

        entry = self._numpy_cache.get(("col64", position))
        if entry is None:
            return
        numpy = fastpath.module()
        if numpy is None:
            return
        cached = entry[1]
        try:
            if slot >= cached.shape[0]:
                raise IndexError(slot)
            values = numpy.frombuffer(payload, dtype=numpy.float32)
            cached[slot, :] = values.astype(numpy.float64)
        except (IndexError, TypeError, ValueError):
            self._numpy_cache.pop(("col64", position), None)

    # -- durability --------------------------------------------------------''')
    path = os.path.join(ROOT, "minivectordb", "store.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "self._refresh_float64(position, slot, payload)" not in text:
        text = text.replace(
            "                self._arena[position].write_at(slot * size, payload)\n",
            "                self._arena[position].write_at(slot * size, payload)\n"
            "                self._refresh_float64(position, slot, payload)\n",
            1)
        print("  store.py: the column mirror follows a write")
    if "    def flush(self) -> None:" not in text:
        text = text.replace(
            "    # -- durability --------------------------------------------------------\n"
            "    def sync(self) -> None:\n",
            "    # -- durability --------------------------------------------------------\n"
            "    def flush(self) -> None:\n"
            "        \"\"\"Hand the buffered tail of every file to the operating system.\n"
            "\n"
            "        The data files are a materialised view of the write-ahead log, so\n"
            "        they do not have to be fsynced to be durable; the log carries that\n"
            "        guarantee.  They do have to reach the file system, because the\n"
            "        vectorised scan reads the columns through a memory mapping.\n"
            "        \"\"\"\n"
            "\n"
            "        self._pk.flush()\n"
            "        self._live.flush()\n"
            "        for arena in self._arena:\n"
            "            arena.flush()\n"
            "        for nulls in self._nulls:\n"
            "            nulls.flush()\n"
            "        for offs in self._offs:\n"
            "            if offs is not None:\n"
            "                offs.flush()\n"
            "        for lens in self._lens:\n"
            "            if lens is not None:\n"
            "                lens.flush()\n"
            "\n"
            "    def sync(self) -> None:\n",
            1)
        print("  store.py: the data files are flushed on commit")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    # -- engine.py --------------------------------------------------------
    path = os.path.join(ROOT, "minivectordb", "engine.py")
    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    if "to_float32_literal" not in text:
        text = text.replace(
            "                   decode_value, encode_row, l2_distance, parse_vector_literal,\n"
            "                   to_float32)\n",
            "                   decode_value, encode_row, l2_distance, parse_vector_literal,\n"
            "                   to_float32, to_float32_literal)\n",
            1)
        text = text.replace(
            "    if isinstance(value, str):\n"
            "        return to_float32(parse_vector_literal(value))\n",
            "    if isinstance(value, str):\n"
            "        return to_float32_literal(value)\n",
            1)
        text = text.replace(
            "    def _sync(self) -> None:\n"
            "        assert self._wal is not None\n"
            "        os.fsync(self._wal.fileno())\n",
            "    def _sync(self) -> None:\n"
            "        assert self._wal is not None\n"
            "        # fdatasync only flushes the data of the log, not the metadata of\n"
            "        # the directory entry, which is what the log needs.  It is not\n"
            "        # available on every platform, so the plain fsync is the fallback.\n"
            "        sync = getattr(os, \"fdatasync\", os.fsync)\n"
            "        sync(self._wal.fileno())\n",
            1)
        text = text.replace(
            "    def _materialize(self) -> None:\n"
            "        \"\"\"Write the committed changes of the overlay into the data files.\"\"\"\n"
            "        for key in self._overlay_order:\n",
            "    def _materialize(self) -> None:\n"
            "        \"\"\"Write the committed changes of the overlay into the data files.\"\"\"\n"
            "        touched: dict[int, Any] = {}\n"
            "        for key in self._overlay_order:\n",
            1)
        text = text.replace(
            "            else:\n"
            "                table.store.put(pk, value)\n"
            "\n"
            "    def _exists(self, table: Table, pk: int) -> bool:\n",
            "            else:\n"
            "                table.store.put(pk, value)\n"
            "            touched[id(table.store)] = table.store\n"
            "        # The rows have to reach the file system before the next query\n"
            "        # reads the columns through a memory mapping.  The write-ahead log\n"
            "        # already carries the durability of the commit, so the data files\n"
            "        # are flushed but not fsynced here.\n"
            "        for store in touched.values():\n"
            "            store.flush()\n"
            "\n"
            "    def _exists(self, table: Table, pk: int) -> bool:\n",
            1)
        print("  engine.py: the literal cache, the log sync and the commit flush")
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    apply("engine.py", "    def _distance_target(self, table: Table, statement: dict, expression: Any):",
          "    def _split_distance(self, table: Table, args: list[Any]):",
          '''    def _distance_target(self, table: Table, statement: dict, expression: Any):
        """Return ``(column position, query vector, query text)``.

        The query text is set when the literal was written as a quoted
        string, which is the shape the task package uses; the caller then
        converts it with the memoised helper.
        """

        if expression[0] == "func" and expression[1] == "L2_DISTANCE":
            return self._split_distance(table, expression[2])
        if expression[0] == "col":
            wanted = expression[1].lower()
            for item in statement["items"]:
                alias = item["alias"]
                if alias is None:
                    alias = _default_alias(item["expr"])
                if alias.lower() != wanted:
                    continue
                if item["expr"][0] == "func" and item["expr"][1] == "L2_DISTANCE":
                    return self._split_distance(table, item["expr"][2])
        return None

    def _distance_expression(self, statement: dict, expression: Any):
        """The ``L2_DISTANCE`` expression the order key refers to."""

        if expression[0] == "func" and expression[1] == "L2_DISTANCE":
            return expression
        if expression[0] == "col":
            wanted = expression[1].lower()
            for item in statement["items"]:
                alias = item["alias"]
                if alias is None:
                    alias = _default_alias(item["expr"])
                if alias.lower() != wanted:
                    continue
                if item["expr"][0] == "func" and item["expr"][1] == "L2_DISTANCE":
                    return item["expr"]
        return None

''')
    apply("engine.py", "    def _split_distance(self, table: Table, args: list[Any]):",
          "    def _project(self, expr: Any, table: Table, row: list[Any]) -> Any:",
          '''    def _split_distance(self, table: Table, args: list[Any]):
        """Split ``L2_DISTANCE(a, b)`` into the vector column and the query.

        The query vector is accepted in both shapes the grammar produces: the
        bare list ``[1, 2]`` is a ``vec`` node, while the quoted form
        ``'[1, 2]'`` is a ``str`` node whose text still holds a vector
        literal.  The quoted form is what the task package uses, so it has to
        reach the vectorised path as well; the text is converted with the
        same helper the generic executor uses, so an invalid literal still
        ends up in the generic path and raises the same error.
        """

        if len(args) != 2:
            return None
        column_position = None
        query = None
        query_text = None
        for arg in args:
            if arg[0] == "col":
                try:
                    column = table.column(arg[1])
                except SqlError:
                    return None
                if column.type != VECTOR:
                    return None
                column_position = table.column_index(arg[1])
            elif arg[0] == "vec":
                query = arg[1]
            elif arg[0] == "str":
                query_text = arg[1]
            else:
                return None
        if column_position is None or (query is None and query_text is None):
            return None
        return column_position, query, query_text

''')
    apply("engine.py", "        target = self._distance_target(table, statement, expression)",
          "        numpy = fastpath.module()",
          '''        target = self._distance_target(table, statement, expression)
        if target is None:
            return None
        column_position, query_values, query_text = target
        column = table.columns[column_position]
        try:
            if query_text is not None:
                query = to_float32_literal(query_text)
            else:
                query = to_float32(query_values)
        except SqlError:
            return None                      # invalid vector: generic path raises
        if column.dim is None or len(query) != column.dim:
            return None                      # the generic path raises the error
''')
    apply("engine.py", "        names, projections = self._select_projections(table, statement)",
          "        # The distances of the selected rows are recomputed with the reference",
          '''        names, projections = self._select_projections(table, statement)
        if int(limit) <= 0:
            return {"columns": names, "rows": []}
        offset = int(statement.get("offset") or 0)
        candidates = self._scan_topk(table, column_position, query,
                                     statement["where"], int(limit) + offset)
        if candidates is None:
            return None
        if not candidates:
            return {"columns": names, "rows": []}

''')
    apply("engine.py", "        # The distances of the selected rows are recomputed with the reference",
          "    # Slots processed per chunk by the vectorised scan.",
          '''        # The distances of the selected rows are recomputed with the reference
        # implementation, so the reported numbers and the tie-break order are
        # exactly the ones a pure-Python scan would have produced.  The
        # projection that shows the distance reuses that value instead of
        # computing it a second time.
        distance_expression = self._distance_expression(statement, expression)
        selected: list[tuple[float, int, list[Any]]] = []
        for _distance, _pk, slot in candidates:
            raw = store.read_slot(slot)
            distance = l2_distance(_as_vector(raw[column_position]), query)
            values = []
            for expr, _alias in projections:
                if distance_expression is not None and expr == distance_expression:
                    values.append(distance)
                else:
                    values.append(self._project(expr, table, raw))
            selected.append((distance, int(store.read_pk(slot)), values))
        selected.sort(key=lambda item: (item[0], item[1]))
        output = [[_json_value(value) for value in values]
                  for _distance, _pk, values in selected[offset:offset + int(limit)]]
        return {"columns": names, "rows": output}

''')
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
