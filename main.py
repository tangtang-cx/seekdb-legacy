#!/usr/bin/env python3
"""Entry point for MiniVectorDB.

    python3 main.py [--data-dir DIR]

The data directory defaults to ``$VECTORDB_DATA_DIR`` and then to ``/data``.
The process speaks the JSON Lines judge protocol on stdin/stdout.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from minivectordb.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
