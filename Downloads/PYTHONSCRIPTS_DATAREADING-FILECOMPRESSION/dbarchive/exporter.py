"""Streaming query-to-CSV export.

Rows are streamed to disk rather than materialized in memory, and the writer
stops early once an optional byte budget is hit, so an export of a huge table
stays bounded in both memory and file size.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

from .database import Database


def rows_to_csv(
    db: Database,
    table: str,
    order_column: str | None,
    where: str | None,
    params: Sequence,
    out_path: Path,
    size_limit_bytes: int | None = None,
) -> tuple[int, int]:
    """Write matching rows of *table* to *out_path* as CSV.

    ``order_column`` (already a bare column name) is validated and quoted through
    ``db.quote_ident``; ``where`` is a caller-built clause using ``?`` params. The
    header comes from the DB result's own keys. Returns ``(row_count, bytes)``.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    quoted_table = db.qualify(table)
    sql = f"SELECT * FROM {quoted_table}"
    if where:
        sql += f" WHERE {where}"
    if order_column:
        sql += f" ORDER BY {db.quote_ident(order_column)} DESC"

    rows = db.query(sql, tuple(params))
    row_count = 0
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if rows:
            header = list(rows[0].keys())
            writer.writerow(header)
            for row in rows:
                writer.writerow([row[col] for col in header])
                row_count += 1
                if size_limit_bytes is not None and handle.tell() >= size_limit_bytes:
                    break
    return row_count, out_path.stat().st_size


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a CSV back into a header and rows (used by the restorer)."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        return header, [row for row in reader]
