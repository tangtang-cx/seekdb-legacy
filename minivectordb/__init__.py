"""MiniVectorDB — a compact AI-native vector database.

Implements the judge protocol of the OceanBase competition task package
(``2026-competition-round2``): JSON Lines over stdin/stdout, ACID storage
under ``/data``, float32 vector semantics and exact/ANN vector search.
"""

__all__ = ["core", "engine", "server", "sqlparser"]
