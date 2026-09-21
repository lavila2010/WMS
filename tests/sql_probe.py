"""SQL statement counter for allocation performance tests. No PII / credentials."""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter

from sqlalchemy import event

from app.extensions import db


@contextmanager
def count_sql():
    stats = {"count": 0, "db_ms": 0.0}
    start = {"t": None}

    def before(_conn, _cursor, _statement, _parameters, _context, _executemany):
        stats["count"] += 1
        start["t"] = perf_counter()

    def after(_conn, _cursor, _statement, _parameters, _context, _executemany):
        if start["t"] is not None:
            stats["db_ms"] += (perf_counter() - start["t"]) * 1000
            start["t"] = None

    event.listen(db.engine, "before_cursor_execute", before)
    event.listen(db.engine, "after_cursor_execute", after)
    try:
        yield stats
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
        event.remove(db.engine, "after_cursor_execute", after)
