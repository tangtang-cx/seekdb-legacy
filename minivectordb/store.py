"""Columnar, log-structured row store backed by files under /data.

Why this exists
---------------
The task states that the formal test set contains scenarios whose data volume
exceeds the memory budget, so a database that keeps every row in RAM cannot
pass.  ``RowStore`` used to be a plain ``dict``; this module replaces it.

Layout
------
Every table owns one directory.  Each physical array lives in its own file so
that a column can be scanned without touching the others:

    pk.i64        int64   per slot   primary key of that slot
    live.u8       uint8   per slot   1 while the slot is visible
    c<i>.dat      bytes              column payload arena
    c<i>.null.u8  uint8   per slot   1 when the value is NULL
    c<i>.off.u64  uint64  per slot   blob offset  (VARCHAR only)
    c<i>.len.u32  uint32  per slot   blob length  (VARCHAR only)

A *slot* is one row version.  Only the primary key index lives in RAM; the
arrays are memory mapped, so the kernel keeps the hot pages in the page cache
and reclaims them when the cgroup limit is reached.  That is what allows the
database to serve a dataset that is larger than the memory budget.

Slot lifetime
-------------
* insert  - appends a slot, the key index points at it
* update  - rewrites the payload of the live slot in place
* delete  - clears the live flag and drops the key from the index

A key that is deleted and inserted again gets a fresh slot; the dead slot is
never reused, which keeps replay of the write-ahead log idempotent.
"""
from __future__ import annotations

import array
import hashlib
import mmap
import os
import struct
from typing import Any, Iterator, Sequence

from .core import BIGINT, VECTOR, VARCHAR, Column
from . import fastpath

_I64 = struct.Struct("<q")
_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")

# mmap.resize() only exists on Unix.  The judge runs Linux; local Windows
# development falls back to plain file reads, which behave identically apart
# from speed.
_HAS_RESIZE = hasattr(mmap.mmap, "resize")

# A file is mapped as soon as it holds a page of data, so the small columns
# (the primary keys, the liveness flags, the offsets) are served from the
# page cache instead of one read syscall per value.
MAP_MIN = 4096

# mmap.resize() costs tens of microseconds, so the mapping of a big file is
# refreshed in coarse chunks instead of on every write.
MAP_CHUNK = 1 << 20

# Upper bound for the cached float64 copy of a vector column.  A bigger
# column is scanned in chunks instead, which keeps the memory bounded.
FLOAT64_CACHE_BYTES = 64 * 1024 * 1024

_EMPTY = -1
_TOMB = -2

# Without O_BINARY, Windows opens the descriptor in text mode and translates
# "\n" into "\r\n" on write.  That silently corrupts the fixed width columns
# (a BIGINT value of 10 is the byte 0x0a) and shifts every later slot.
_O_BINARY = getattr(os, "O_BINARY", 0)


class ByteFile:
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
        return self._size


class PkIndex:
    """Open-addressing hash map from primary key to slot.

    ``array('q')`` keeps the footprint at roughly 32 bytes per row, which is
    what makes a multi-million row table fit inside the memory budget.  A
    plain ``dict`` would need three times as much and would push the process
    over the cgroup limit.
    """

    __slots__ = ("_keys", "_slots", "_mask", "_used", "_tomb")

    def __init__(self, capacity: int = 1024) -> None:
        self._keys = array.array("q", bytes(8 * capacity))
        self._slots = array.array("q", bytes(8 * capacity))
        for i in range(capacity):
            self._slots[i] = _EMPTY
        self._mask = capacity - 1
        self._used = 0
        self._tomb = 0

    def __len__(self) -> int:
        return self._used

    @staticmethod
    def _hash(key: int, mask: int) -> int:
        h = (key * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        h ^= h >> 29
        h = (h * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        h ^= h >> 32
        return h & mask

    def get(self, key: int) -> int | None:
        slots = self._slots
        index = self._hash(key, self._mask)
        while True:
            slot = slots[index]
            if slot == _EMPTY:
                return None
            if slot != _TOMB and self._keys[index] == key:
                return slot
            index = (index + 1) & self._mask

    def put(self, key: int, slot: int) -> None:
        if (self._used + self._tomb + 1) * 10 >= len(self._slots) * 7:
            self._grow()
        index = self._hash(key, self._mask)
        slots = self._slots
        first_free = -1
        while True:
            current = slots[index]
            if current == _EMPTY:
                if first_free >= 0:
                    slots[first_free] = slot
                    self._keys[first_free] = key
                    self._tomb -= 1
                else:
                    slots[index] = slot
                    self._keys[index] = key
                    self._used += 1
                return
            if current == _TOMB:
                if first_free < 0:
                    first_free = index
            elif self._keys[index] == key:
                slots[index] = slot
                return
            index = (index + 1) & self._mask

    def remove(self, key: int) -> bool:
        slots = self._slots
        index = self._hash(key, self._mask)
        while True:
            current = slots[index]
            if current == _EMPTY:
                return False
            if current != _TOMB and self._keys[index] == key:
                slots[index] = _TOMB
                self._used -= 1
                self._tomb += 1
                return True
            index = (index + 1) & self._mask

    def _grow(self) -> None:
        capacity = len(self._slots) * 2
        keys = array.array("q", bytes(8 * capacity))
        slots = array.array("q", bytes(8 * capacity))
        for i in range(capacity):
            slots[i] = _EMPTY
        mask = capacity - 1
        used = 0
        for i, slot in enumerate(self._slots):
            if slot >= 0:
                key = self._keys[i]
                index = PkIndex._hash(key, mask)
                while slots[index] != _EMPTY:
                    index = (index + 1) & mask
                slots[index] = slot
                keys[index] = key
                used += 1
        self._keys = keys
        self._slots = slots
        self._mask = mask
        self._used = used
        self._tomb = 0

    def rebuild(self, keys: Sequence[int], slots: Sequence[int]) -> None:
        """Reset the map from an explicit key/slot list."""
        capacity = 1024
        while (len(keys) + 1) * 10 >= capacity * 7:
            capacity *= 2
        self._keys = array.array("q", bytes(8 * capacity))
        self._slots = array.array("q", bytes(8 * capacity))
        for i in range(capacity):
            self._slots[i] = _EMPTY
        self._mask = capacity - 1
        self._used = 0
        self._tomb = 0
        for key, slot in zip(keys, slots):
            self.put(key, slot)


def table_directory(data_dir: str, table_name: str) -> str:
    """Deterministic directory name for a table (case insensitive)."""
    digest = hashlib.sha1(table_name.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(data_dir, "tables", digest)


class ColumnarStore:
    """Disk backed row storage with the interface the executor expects."""

    def __init__(self, directory: str, columns: Sequence[Column]) -> None:
        self.directory = directory
        self.columns = list(columns)
        if not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        self._pk = ByteFile(os.path.join(directory, "pk.i64"))
        self._live = ByteFile(os.path.join(directory, "live.u8"))
        self._arena: list[ByteFile] = []
        self._nulls: list[ByteFile] = []
        self._offs: list[ByteFile | None] = []
        self._lens: list[ByteFile | None] = []
        for position, column in enumerate(self.columns):
            self._arena.append(ByteFile(os.path.join(directory, f"c{position}.dat")))
            self._nulls.append(ByteFile(os.path.join(directory, f"c{position}.null.u8")))
            if column.type == VARCHAR:
                self._offs.append(ByteFile(os.path.join(directory, f"c{position}.off.u64")))
                self._lens.append(ByteFile(os.path.join(directory, f"c{position}.len.u32")))
            else:
                self._offs.append(None)
                self._lens.append(None)
        self._index = PkIndex()
        self._numpy_cache: dict = {}
        self._rebuild_index()

    # -- geometry ----------------------------------------------------------
    @property
    def slot_count(self) -> int:
        return self._pk.size // 8

    def _element_size(self, column: Column) -> int:
        if column.type == BIGINT:
            return 8
        if column.type == VECTOR:
            return max(0, (column.dim or 0)) * 4
        return 0

    # -- index -------------------------------------------------------------
    def _rebuild_index(self) -> None:
        count = self.slot_count
        if count == 0:
            self._index.rebuild([], [])
            return
        live = self._live.read(0, count)
        keys = array.array("q")
        slots = array.array("q")
        position = live.find(b"\x01")
        while position >= 0:
            keys.append(_I64.unpack(self._pk.read(position * 8, 8))[0])
            slots.append(position)
            position = live.find(b"\x01", position + 1)
        self._index.rebuild(keys, slots)

    # -- reads -------------------------------------------------------------
    def exists(self, pk: int) -> bool:
        return self._index.get(pk) is not None

    def slot_of(self, pk: int) -> int | None:
        """Slot that holds the live version of ``pk``, if any."""
        return self._index.get(pk)

    def restore_slot(self, slot: int, values: Sequence[Any]) -> None:
        """Put ``values`` back into ``slot`` and make the slot visible again.

        Used by the undo log: the slot index is stable for rows that existed
        before the transaction started, so the previous content can be
        restored in place.
        """
        self._write_slot(slot, values)
        self._live.write_at(slot, b"\x01")

    def get(self, pk: int) -> list[Any] | None:
        slot = self._index.get(pk)
        if slot is None:
            return None
        return self._read_slot(slot)

    def _read_slot(self, slot: int) -> list[Any]:
        values: list[Any] = []
        for position, column in enumerate(self.columns):
            if self._nulls[position].read(slot, 1)[0]:
                values.append(None)
                continue
            if column.type == BIGINT:
                values.append(_I64.unpack(self._arena[position].read(slot * 8, 8))[0])
            elif column.type == VECTOR:
                size = (column.dim or 0) * 4
                raw = self._arena[position].read(slot * size, size)
                vec = array.array("f")
                vec.frombytes(raw)
                values.append(vec)
            else:
                offset = _U64.unpack(self._offs[position].read(slot * 8, 8))[0]
                length = _U32.unpack(self._lens[position].read(slot * 4, 4))[0]
                values.append(self._arena[position].read(offset, length).decode("utf-8"))
        return values

    def iter_rows(self) -> Iterator[tuple[int, list[Any]]]:
        """Stream every live row in slot (insertion) order."""
        count = self.slot_count
        live = self._live.read(0, count)
        for slot in range(count):
            if not live[slot]:
                continue
            pk = _I64.unpack(self._pk.read(slot * 8, 8))[0]
            yield pk, self._read_slot(slot)

    def iter_slots(self) -> Iterator[int]:
        count = self.slot_count
        live = self._live.read(0, count)
        for slot in range(count):
            if live[slot]:
                yield slot

    def pks(self) -> Iterator[int]:
        for slot in self.iter_slots():
            yield _I64.unpack(self._pk.read(slot * 8, 8))[0]

    def read_pk(self, slot: int) -> int:
        return _I64.unpack(self._pk.read(slot * 8, 8))[0]

    def read_slot(self, slot: int) -> list[Any]:
        return self._read_slot(slot)

    # -- numpy views ------------------------------------------------------
    #
    # The vectorised TopK scan reads whole columns at once.  These helpers
    # expose the columnar files as numpy arrays; they return ``None`` when
    # numpy is unavailable or the column is not fixed width.
    #
    # The fixed width columns are memory mapped rather than read into a
    # buffer, so the resident memory of a scan does not grow with the size
    # of the table.  The pages the scan touches are served from the page
    # cache and can be reclaimed, which is what keeps a table that is
    # larger than the memory budget searchable.

    def _mapped(self, key: tuple, path: str, dtype: str, elements: int,
                per_slot: int = 1, size: int | None = None) -> Any:
        """A cached numpy view of a column file.

        The size is taken from the arena when the caller knows it, so the
        cache does not have to ask the file system on every query.
        """

        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                return None
        entry = self._numpy_cache.get(key)
        if entry is not None and entry[0] == size:
            return entry[1]
        view = fastpath.map_column(path, dtype, elements, per_slot)
        if view is None:
            self._numpy_cache.pop(key, None)
            return None
        self._numpy_cache[key] = (size, view)
        return view

    def fixed_column_array(self, position: int) -> Any:
        """A fixed width column as a numpy array, one value per slot.

        ``BIGINT`` columns return an ``int64`` array, ``VECTOR`` columns a
        ``float32`` matrix with one row per slot.
        """

        if not fastpath.available():
            return None
        column = self.columns[position]
        count = self.slot_count
        if column.type == BIGINT:
            return self._mapped(("col", position), self._arena[position].path,
                                "<i8", count, 1, self._arena[position].size)
        if column.type == VECTOR:
            dim = column.dim or 0
            if dim <= 0:
                return None
            return self._mapped(("col", position), self._arena[position].path,
                                "<f4", count, dim, self._arena[position].size)
        return None

    def vector_matrix64(self, position: int) -> Any:
        """The vector column as a ``float64`` matrix, cached between queries.

        The distances are accumulated in float64, so the scan needs the
        values in that precision.  Converting the column once and keeping
        the result removes the per query conversion, which is the most
        expensive part of the scan.  The cache is only used while it stays
        inside ``FLOAT64_CACHE_BYTES``; a bigger table is scanned in
        chunks instead, which keeps the memory bounded.
        """

        if not fastpath.available():
            return None
        column = self.columns[position]
        dim = column.dim or 0
        count = self.slot_count
        if dim <= 0 or count == 0:
            return None
        if count * dim * 8 > FLOAT64_CACHE_BYTES:
            return None
        key = ("col64", position)
        try:
            size = self._arena[position].size
        except OSError:
            return None
        entry = self._numpy_cache.get(key)
        if entry is not None and entry[0] == size:
            return entry[1]
        source = self.fixed_column_array(position)
        if source is None:
            return None
        numpy = fastpath.module()
        converted = numpy.array(source, dtype=numpy.float64)
        self._numpy_cache[key] = (size, converted)
        return converted

    def vector_norms64(self, position: int) -> Any:
        """The squared norm of every row of a vector column, in float64.

        The scan can turn the distances into one matrix product when the
        squared norm of every row is known: `|a-q|^2 = |a|^2 - 2 a.q + |q|^2`.
        The result differs from the reference distance by a few ulp, and by
        about 1e-7 when the query is identical to a row and the two terms
        cancel; the candidate selection keeps a tolerance that covers it, and
        the rows that are reported are recomputed with the reference
        implementation.
        """

        if not fastpath.available():
            return None
        matrix = self.vector_matrix64(position)
        if matrix is None:
            return None
        try:
            size = self._arena[position].size
        except OSError:
            return None
        key = ("norm64", position)
        entry = self._numpy_cache.get(key)
        if entry is not None and entry[0] == size:
            return entry[1]
        numpy = fastpath.module()
        norms = numpy.einsum("ij,ij->i", matrix, matrix)
        self._numpy_cache[key] = (size, norms)
        return norms

    def null_mask(self, position: int) -> Any:
        """A boolean array that is ``True`` where the slot holds NULL."""

        if not fastpath.available():
            return None
        flags = self._mapped(("null", position), self._nulls[position].path,
                             "u1", self.slot_count, 1, self._nulls[position].size)
        if flags is None:
            return None
        return flags.astype(bool)

    def live_mask(self) -> Any:
        """A boolean array that is ``True`` where the slot holds a live row."""

        if not fastpath.available():
            return None
        flags = self._mapped(("live",), self._live.path, "u1", self.slot_count,
                             1, self._live.size)
        if flags is None:
            return None
        return flags.astype(bool)

    def primary_keys(self) -> Any:
        """The primary key of every slot as an ``int64`` array."""

        if not fastpath.available():
            return None
        return self._mapped(("pk",), self._pk.path, "<i8", self.slot_count,
                             1, self._pk.size)

    def varchar_column_bytes(self, position: int, start: int = 0,
                             end: int | None = None) -> tuple[Any, Any, Any]:
        """``(arena, offsets, lengths)`` for a VARCHAR column.

        When a slot range is given, only the arena bytes that the range
        needs are read and the offsets are rebased to that slice, so a
        chunked scan never holds the whole string column in memory.
        """

        if not fastpath.available():
            return None, None, None
        count = self.slot_count
        offsets = self._mapped(("off", position), self._offs[position].path,
                               "<u8", count)
        lengths = self._mapped(("len", position), self._lens[position].path,
                               "<u4", count)
        if offsets is None or lengths is None:
            return None, None, None
        if end is None and start == 0:
            return self._arena[position].read_all(), offsets, lengths

        numpy = fastpath.module()
        end = count if end is None else min(int(end), count)
        start = max(0, min(int(start), end))
        chunk_offsets = offsets[start:end]
        chunk_lengths = lengths[start:end]
        present = numpy.ones(chunk_offsets.shape[0], dtype=bool)
        nulls = self.null_mask(position)
        if nulls is not None:
            present = ~nulls[start:end]
        if not bool(present.any()):
            return b"", chunk_offsets, chunk_lengths
        starts = chunk_offsets[present]
        ends = starts + chunk_lengths[present]
        low = int(starts.min())
        high = int(ends.max())
        arena = self._arena[position].read(low, high - low)
        rebased = chunk_offsets - low
        return arena, rebased, chunk_lengths

    # -- writes ------------------------------------------------------------
    def put(self, pk: int, values: Sequence[Any]) -> None:
        slot = self._index.get(pk)
        if slot is None:
            slot = self.slot_count
            self._pk.append(_I64.pack(pk))
            self._live.append(b"\x01")
            for position in range(len(self.columns)):
                self._nulls[position].append(b"\x00")
                if self.columns[position].type == VARCHAR:
                    self._offs[position].append(_U64.pack(0))
                    self._lens[position].append(_U32.pack(0))
            self._index.put(pk, slot)
        self._write_slot(slot, values)

    def _write_slot(self, slot: int, values: Sequence[Any]) -> None:
        for position, column in enumerate(self.columns):
            value = values[position] if position < len(values) else None
            if value is None:
                self._nulls[position].write_at(slot, b"\x01")
                # Fixed size columns still advance their arena so that the next
                # slot keeps a stable offset.
                if column.type == BIGINT:
                    self._arena[position].write_at(slot * 8, b"\x00" * 8)
                elif column.type == VECTOR:
                    size = (column.dim or 0) * 4
                    if size:
                        self._arena[position].write_at(slot * size, b"\x00" * size)
                continue
            self._nulls[position].write_at(slot, b"\x00")
            if column.type == BIGINT:
                self._arena[position].write_at(slot * 8, _I64.pack(int(value)))
            elif column.type == VECTOR:
                size = (column.dim or 0) * 4
                payload = value.tobytes() if hasattr(value, "tobytes") else array.array("f", value).tobytes()
                if len(payload) != size:
                    payload = payload[:size].ljust(size, b"\x00")
                self._arena[position].write_at(slot * size, payload)
                self._refresh_float64(position, slot, payload)
            else:
                encoded = value.encode("utf-8")
                offset = self._arena[position].append(encoded)
                self._offs[position].write_at(slot * 8, _U64.pack(offset))
                self._lens[position].write_at(slot * 4, _U32.pack(len(encoded)))

    def delete(self, pk: int) -> None:
        slot = self._index.get(pk)
        if slot is None:
            return
        self._live.write_at(slot, b"\x00")
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
            widened = values.astype(numpy.float64)
            cached[slot, :] = widened
            norms = self._numpy_cache.get(("norm64", position))
            if norms is not None:
                norms[1][slot] = float(numpy.dot(widened, widened))
        except (IndexError, TypeError, ValueError):
            self._numpy_cache.pop(("col64", position), None)
            self._numpy_cache.pop(("norm64", position), None)

    # -- durability --------------------------------------------------------
    def flush(self) -> None:
        """Hand the buffered tail of every file to the operating system.

        The data files are a materialised view of the write-ahead log, so
        they do not have to be fsynced to be durable; the log carries that
        guarantee.  They do have to reach the file system, because the
        vectorised scan reads the columns through a memory mapping.
        """

        self._pk.flush()
        self._live.flush()
        for arena in self._arena:
            arena.flush()
        for nulls in self._nulls:
            nulls.flush()
        for offs in self._offs:
            if offs is not None:
                offs.flush()
        for lens in self._lens:
            if lens is not None:
                lens.flush()

    def sync(self) -> None:
        self._pk.sync()
        self._live.sync()
        for arena in self._arena:
            arena.sync()
        for nulls in self._nulls:
            nulls.sync()
        for offs in self._offs:
            if offs is not None:
                offs.sync()
        for lens in self._lens:
            if lens is not None:
                lens.sync()

    def close(self) -> None:
        self._numpy_cache.clear()
        for handle in [self._pk, self._live, *self._arena, *self._nulls,
                       *(o for o in self._offs if o is not None),
                       *(l for l in self._lens if l is not None)]:
            handle.close()

    # -- checkpointing -----------------------------------------------------
    def checkpoint_state(self) -> dict:
        state = {"slots": self.slot_count,
                 "pk": self._pk.size,
                 "live": self._live.size,
                 "arena": [], "nulls": [], "off": [], "len": []}
        for position in range(len(self.columns)):
            state["arena"].append(self._arena[position].size)
            state["nulls"].append(self._nulls[position].size)
            state["off"].append(self._offs[position].size if self._offs[position] else 0)
            state["len"].append(self._lens[position].size if self._lens[position] else 0)
        return state

    def restore_state(self, state: dict) -> None:
        """Roll the files back to a checkpointed shape.

        Slots beyond the checkpoint are dropped; they are recreated when the
        write-ahead log is replayed.
        """
        self._pk.truncate(int(state.get("pk", 0)))
        self._live.truncate(int(state.get("live", 0)))
        for position in range(len(self.columns)):
            arena = state.get("arena", [])
            nulls = state.get("nulls", [])
            offs = state.get("off", [])
            lens = state.get("len", [])
            if position < len(arena):
                self._arena[position].truncate(int(arena[position]))
            if position < len(nulls):
                self._nulls[position].truncate(int(nulls[position]))
            if position < len(offs) and self._offs[position] is not None:
                self._offs[position].truncate(int(offs[position]))
            if position < len(lens) and self._lens[position] is not None:
                self._lens[position].truncate(int(lens[position]))
        self._rebuild_index()
