"""Archive catalog and compression-log persistence.

Two audit tables live in the same database as the data:

- the **catalog** is the authoritative record of every archive — its identity,
  provenance, sizes, checksum, and lifecycle status;
- the **compression log** is a lighter, append-only trail of each compression
  event, kept separate so log reads never contend with catalog updates.

Both are created on demand so a read before the first archive returns an empty
list instead of a "no such table" error.
"""

from __future__ import annotations

from .config import Config
from .database import Database
from .models import ArchiveRecord

_CATALOG_COLUMNS = [
    "batch_id", "source_table", "archive_table", "filename", "path",
    "start_ts", "end_ts", "rows", "csv_bytes", "gz_bytes", "ratio",
    "checksum", "created_at", "status", "notes",
]


def _catalog_ddl(db: Database, table: str) -> str:
    ident = db.quote_ident(table)
    if db.dialect == "sqlserver":  # pragma: no cover - needs SQL Server
        return (
            f"CREATE TABLE {ident} ("
            "batch_id NVARCHAR(64) NOT NULL PRIMARY KEY, "
            "source_table NVARCHAR(200) NOT NULL, archive_table NVARCHAR(200) NOT NULL, "
            "filename NVARCHAR(255) NOT NULL, path NVARCHAR(1000) NOT NULL, "
            "start_ts NVARCHAR(40) NULL, end_ts NVARCHAR(40) NULL, "
            "rows INT NOT NULL, csv_bytes BIGINT NOT NULL, gz_bytes BIGINT NOT NULL, "
            "ratio DECIMAL(6,2) NOT NULL, checksum NVARCHAR(64) NOT NULL, "
            "created_at NVARCHAR(40) NOT NULL, status NVARCHAR(20) NOT NULL, "
            "notes NVARCHAR(1000) NULL)"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {ident} ("
        "batch_id TEXT NOT NULL PRIMARY KEY, "
        "source_table TEXT NOT NULL, archive_table TEXT NOT NULL, "
        "filename TEXT NOT NULL, path TEXT NOT NULL, "
        "start_ts TEXT, end_ts TEXT, rows INTEGER NOT NULL, "
        "csv_bytes INTEGER NOT NULL, gz_bytes INTEGER NOT NULL, "
        "ratio REAL NOT NULL, checksum TEXT NOT NULL, "
        "created_at TEXT NOT NULL, status TEXT NOT NULL, notes TEXT)"
    )


def _logs_ddl(db: Database, table: str) -> str:
    ident = db.quote_ident(table)
    if db.dialect == "sqlserver":  # pragma: no cover - needs SQL Server
        return (
            f"CREATE TABLE {ident} ("
            "log_id INT IDENTITY(1,1) PRIMARY KEY, batch_id NVARCHAR(64) NOT NULL, "
            "source_table NVARCHAR(200) NOT NULL, filename NVARCHAR(255) NOT NULL, "
            "rows INT NOT NULL, csv_bytes BIGINT NOT NULL, gz_bytes BIGINT NOT NULL, "
            "ratio DECIMAL(6,2) NOT NULL, created_at NVARCHAR(40) NOT NULL)"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {ident} ("
        "log_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, "
        "source_table TEXT NOT NULL, filename TEXT NOT NULL, rows INTEGER NOT NULL, "
        "csv_bytes INTEGER NOT NULL, gz_bytes INTEGER NOT NULL, ratio REAL NOT NULL, "
        "created_at TEXT NOT NULL)"
    )


def _history_ddl(db: Database, table: str) -> str:
    ident = db.quote_ident(table)
    if db.dialect == "sqlserver":  # pragma: no cover - needs SQL Server
        return (
            f"CREATE TABLE {ident} ("
            "history_id INT IDENTITY(1,1) PRIMARY KEY, batch_id NVARCHAR(64) NOT NULL, "
            "source_table NVARCHAR(200) NOT NULL, threshold_days DECIMAL(10,2) NOT NULL, "
            "rows_archived INT NOT NULL, action NVARCHAR(20) NOT NULL, "
            "created_at NVARCHAR(40) NOT NULL)"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {ident} ("
        "history_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, "
        "source_table TEXT NOT NULL, threshold_days REAL NOT NULL, "
        "rows_archived INTEGER NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)"
    )


def _table_exists(db: Database, table: str) -> bool:
    if db.dialect == "sqlite":
        return bool(
            db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        )
    return bool(  # pragma: no cover - needs SQL Server
        db.query("SELECT 1 FROM sys.tables WHERE name = ?", (table,))
    )


def ensure_tables(db: Database, cfg: Config) -> None:
    catalog = cfg.get_str("catalog_table")
    logs = cfg.get_str("compression_logs_table")
    history = cfg.get_str("archive_history_table")
    if not _table_exists(db, catalog):
        db.execute(_catalog_ddl(db, catalog))
    if not _table_exists(db, logs):
        db.execute(_logs_ddl(db, logs))
    if not _table_exists(db, history):
        db.execute(_history_ddl(db, history))


def insert_archive_history(
    db: Database, cfg: Config, rec: ArchiveRecord, *, threshold_days: float, keep_source: bool
) -> None:
    """Record one audit-trail row for this archive run.

    This is deliberately separate from ``insert_archive`` (the detailed
    catalog entry with checksum/path/sizes): the history table is a lighter
    "what happened, to which table, and was the source kept or deleted"
    trail, meant for a quick human-readable review of past runs.
    """
    table = db.quote_ident(cfg.get_str("archive_history_table"))
    cols = ["batch_id", "source_table", "threshold_days", "rows_archived", "action", "created_at"]
    quoted = ", ".join(db.quote_ident(c) for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    action = "ARCHIVED_KEPT_SOURCE" if keep_source else "ARCHIVED_DELETED_SOURCE"
    db.execute(
        f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
        [rec.batch_id, rec.source_table, threshold_days, rec.rows, action, rec.created_at],
    )


def insert_archive(db: Database, cfg: Config, rec: ArchiveRecord) -> None:
    table = db.quote_ident(cfg.get_str("catalog_table"))
    cols = ", ".join(db.quote_ident(c) for c in _CATALOG_COLUMNS)
    placeholders = ", ".join(["?"] * len(_CATALOG_COLUMNS))
    row = rec.to_row()
    db.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
        [row[c] for c in _CATALOG_COLUMNS],
    )


def insert_compression_log(db: Database, cfg: Config, rec: ArchiveRecord) -> None:
    table = db.quote_ident(cfg.get_str("compression_logs_table"))
    cols = ["batch_id", "source_table", "filename", "rows", "csv_bytes", "gz_bytes", "ratio", "created_at"]
    quoted = ", ".join(db.quote_ident(c) for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    db.execute(
        f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
        [rec.batch_id, rec.source_table, rec.filename, rec.rows,
         rec.csv_bytes, rec.gz_bytes, rec.ratio, rec.created_at],
    )


def query_catalog(db: Database, cfg: Config, filters: dict | None = None) -> list[ArchiveRecord]:
    ensure_tables(db, cfg)
    table = db.quote_ident(cfg.get_str("catalog_table"))
    sql = f"SELECT * FROM {table}"
    conditions: list[str] = []
    params: list = []
    if filters:
        if filters.get("batch_id"):
            conditions.append(f"{db.quote_ident('batch_id')} = ?")
            params.append(filters["batch_id"])
        if filters.get("source_table"):
            conditions.append(f"{db.quote_ident('source_table')} = ?")
            params.append(filters["source_table"])
        if filters.get("status"):
            conditions.append(f"{db.quote_ident('status')} = ?")
            params.append(filters["status"])
        if filters.get("filename"):
            conditions.append(f"{db.quote_ident('filename')} LIKE ?")
            params.append(f"%{filters['filename']}%")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {db.quote_ident('created_at')} DESC"
    return [ArchiveRecord.from_row(r) for r in db.query(sql, params)]


def get_by_batch_id(db: Database, cfg: Config, batch_id: str) -> ArchiveRecord | None:
    rows = query_catalog(db, cfg, {"batch_id": batch_id})
    return rows[0] if rows else None


def query_logs(db: Database, cfg: Config, filters: dict | None = None) -> list[dict]:
    ensure_tables(db, cfg)
    table = db.quote_ident(cfg.get_str("compression_logs_table"))
    sql = f"SELECT * FROM {table}"
    conditions: list[str] = []
    params: list = []
    if filters:
        if filters.get("batch_id"):
            conditions.append(f"{db.quote_ident('batch_id')} = ?")
            params.append(filters["batch_id"])
        if filters.get("filename"):
            conditions.append(f"{db.quote_ident('filename')} LIKE ?")
            params.append(f"%{filters['filename']}%")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {db.quote_ident('created_at')} DESC"
    return db.query(sql, params)


def update_status(db: Database, cfg: Config, batch_id: str, status: str, notes: str | None = None) -> None:
    table = db.quote_ident(cfg.get_str("catalog_table"))
    sql = f"UPDATE {table} SET {db.quote_ident('status')} = ?"
    params: list = [status]
    if notes is not None:
        sql += f", {db.quote_ident('notes')} = ?"
        params.append(notes)
    sql += f" WHERE {db.quote_ident('batch_id')} = ?"
    params.append(batch_id)
    db.execute(sql, params)
