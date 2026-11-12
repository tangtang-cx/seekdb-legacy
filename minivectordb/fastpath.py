"""Optional numpy acceleration for the vector TopK scan.

The judge image installs numpy, but the program has to keep working when the
install failed, so every helper degrades gracefully: the module reports
``available() is False`` and the executor simply keeps using the generic
pure-Python path.

The helpers never change the semantics of a query:

* distances are computed in float64 over the stored float32 values, exactly
  like the reference ``l2_distance``;
* the caller re-computes the distances of the selected rows with the reference
  implementation, so the reported numbers and the tie-break order are the ones
  a pure-Python scan would have produced.
"""

from __future__ import annotations

import os
from typing import Any

try:  # pragma: no cover - depends on the image
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the image
    _np = None  # type: ignore[assignment]

NUMPY_AVAILABLE = _np is not None


def _disabled() -> bool:
    """Whether the vectorised path was switched off on purpose.

    The Docker image falls back to the pure standard library path when numpy
    cannot be installed at build time.  The environment variable makes that
    fallback reproducible on a machine that does have numpy, so the two paths
    can be compared.
    """

    return bool(os.environ.get("MINIVECTORDB_DISABLE_NUMPY"))


def available() -> bool:
    """Whether the vectorised path can be used."""

    if _disabled():
        return False
    return NUMPY_AVAILABLE


def module() -> Any:
    """The numpy module, or ``None`` when it is unavailable."""

    if _disabled():
        return None
    return _np


def map_column(path: str, dtype: str, elements: int, per_slot: int = 1) -> Any:
    """Memory map a fixed width column file.

    The file holds ``elements`` items of ``per_slot`` values each.  Mapping
    instead of reading keeps the resident memory bounded: the operating
    system serves the pages from the page cache and reclaims them under
    pressure, so a table that is larger than the memory budget stays
    searchable.  ``None`` is returned when the file cannot be mapped.
    """

    if _np is None:
        return None
    itemsize = _np.dtype(dtype).itemsize
    needed = elements * per_slot * itemsize
    try:
        if os.path.getsize(path) < needed:
            return None
    except OSError:
        return None
    try:
        flat = _np.memmap(path, dtype=dtype, mode="r", shape=(elements * per_slot,))
    except (OSError, ValueError):
        return None
    if per_slot == 1:
        return flat
    return flat.reshape(elements, per_slot)


def load_fixed_column(path: str, dtype: str, elements: int) -> Any:
    """Read a fixed width column file into a numpy array of ``elements`` items.

    The arena of a fixed width column holds one value per slot, so the file is
    a plain array.  A short file is padded with zeros, which keeps the shape
    stable while the table is being filled.
    """

    if _np is None:
        return None
    raw = _np.fromfile(path, dtype=dtype)
    if raw.size < elements:
        raw = _np.concatenate([raw, _np.zeros(elements - raw.size, dtype=dtype)])
    return raw[:elements]


def load_live_mask(path: str, count: int) -> Any:
    """Read the one byte per slot liveness flag."""

    if _np is None:
        return None
    raw = _np.fromfile(path, dtype=_np.uint8)
    if raw.size < count:
        raw = _np.concatenate([raw, _np.zeros(count - raw.size, dtype=_np.uint8)])
    return raw[:count]


def l2_distances(matrix: Any, query: Any) -> Any:
    """Euclidean distance of every row of ``matrix`` to ``query``.

    ``matrix`` holds float32 or float64 values, ``query`` is the float32
    query vector.  The accumulation happens in float64, matching the
    reference implementation.  The difference is taken before the squaring,
    so no cancellation can happen and a near zero distance stays accurate.
    """

    if _np is None:
        return None
    diff = matrix.astype(_np.float64, copy=False)
    diff = diff - query.astype(_np.float64, copy=False)
    squared = _np.einsum("ij,ij->i", diff, diff)
    return _np.sqrt(squared)


def l2_distances_into(matrix: Any, query: Any, scratch: Any) -> Any:
    """``l2_distances`` with a reusable difference buffer.

    Allocating the difference for every chunk costs a fresh multi megabyte
    allocation each time, which is the most expensive part of the scan.  The
    caller keeps the buffer alive, so the scan only pays for the memory it
    touches.  ``matrix`` has to be float64 so that the subtraction runs in
    float64; the result is identical to ``l2_distances``.
    """

    if _np is None:
        return None
    _np.subtract(matrix, query.astype(_np.float64, copy=False), out=scratch)
    squared = _np.einsum("ij,ij->i", scratch, scratch)
    return _np.sqrt(squared)


def select_topk(distances: Any, pks: Any, limit: int) -> Any:
    """Indices of the ``limit`` closest rows, ordered by (distance, primary key).

    ``argpartition`` narrows the field down in linear time; every row that ties
    with the last selected distance is kept so the primary key tie-break can
    still see all of them.
    """

    if _np is None:
        return None
    total = distances.shape[0]
    if total == 0:
        return _np.zeros(0, dtype=_np.int64)
    take = max(0, min(int(limit), total))
    if take == 0:
        return _np.zeros(0, dtype=_np.int64)
    if take < total:
        partition = _np.argpartition(distances, take - 1)[:take]
        boundary = distances[partition].max()
        # The tie is decided on a tolerance rather than on an exact
        # comparison: the vectorised distance and the reference distance
        # sum the same squares in a different order, so two rows whose
        # reference distances are equal can end up a few ulp apart in the
        # vectorised result.  The rows that survive are ordered by the
        # reference distance again before the answer is returned.
        tolerance = max(1e-6, abs(float(boundary)) * 1e-9)
        candidates = _np.flatnonzero(distances <= boundary + tolerance)
    else:
        candidates = _np.arange(total, dtype=_np.int64)
    order = _np.lexsort((pks[candidates], distances[candidates]))
    return candidates[order][:take]


def compare_bytes(arena: bytes, offsets: Any, lengths: Any, literal: bytes, operator: str) -> Any:
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
    return mask


__all__ = [
    "NUMPY_AVAILABLE",
    "available",
    "compare_bytes",
    "l2_distances",
    "load_fixed_column",
    "load_live_mask",
    "module",
    "select_topk",
]
