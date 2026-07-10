"""Schema discovery — dialect-specific SQL, one shared output shape."""

from __future__ import annotations

from .database import Database, is_temporal_type
from .errors import SchemaError
from .models import ColumnInfo, TableInfo


def list_tables(db: Database) -> list[str]:
    if db.dialect == "sqlite":
        rows = db.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r["name"] for r in rows]
    rows = db.query(  # pragma: no cover - needs SQL Server
        "SELECT s.name + '.' + t.name AS name "
        "FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id "
        "ORDER BY name"
    )
    return [r["name"] for r in rows]


def _row_count(db: Database, table: str) -> int:
    quoted = db.qualify(table)
    return int(db.query(f"SELECT COUNT(*) AS c FROM {quoted}")[0]["c"])


def describe_table(db: Database, table: str) -> TableInfo:
    """Return the table's columns and row count, or raise if it is missing."""
    if db.dialect == "sqlite":
        info = db.query(f"PRAGMA table_info({db.qualify(table)})")
        if not info:
            raise SchemaError(
                f"Table {table!r} was not found in the demo database. "
                f"Run 'demo-init' first, or check the table name."
            )
        columns = [
            ColumnInfo(
                name=row["name"],
                data_type=row["type"] or "",
                nullable=(row["notnull"] == 0),
                is_temporal=is_temporal_type(row["type"] or ""),
            )
            for row in info
        ]
    else:  # pragma: no cover - needs SQL Server
        parts = table.split(".")
        schema_name, table_name = (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])
        info = db.query(
            "SELECT c.name AS name, ty.name AS type, c.is_nullable AS is_nullable "
            "FROM sys.columns c "
            "JOIN sys.types ty ON c.user_type_id = ty.user_type_id "
            "JOIN sys.tables t ON c.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE s.name = ? AND t.name = ? ORDER BY c.column_id",
            (schema_name, table_name),
        )
        if not info:
            raise SchemaError(f"Table {table!r} was not found on the SQL Server database.")
        columns = [
            ColumnInfo(
                name=row["name"],
                data_type=row["type"] or "",
                nullable=bool(row["is_nullable"]),
                is_temporal=is_temporal_type(row["type"] or ""),
            )
            for row in info
        ]

    return TableInfo(name=table, columns=columns, row_count=_row_count(db, table))


def inspect_database(db: Database) -> dict[str, object]:
    """Return a compact summary of database capacity and shape."""
    tables = [describe_table(db, table) for table in list_tables(db)]
    total_rows = sum(table.row_count for table in tables)
    table_sizes = [(table.name, table.row_count) for table in tables]
    largest = max(table_sizes, key=lambda item: item[1], default=("(none)", 0))
    smallest = min(table_sizes, key=lambda item: item[1], default=("(none)", 0))

    if db.dialect == "sqlite":
        view_count = int(
            db.query(
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type = 'view'"
            )[0]["c"]
        )
        proc_count = 0
        index_count = sum(
            len(db.query(f"PRAGMA index_list({db.quote_ident(table.name)})"))
            for table in tables
        )
    else:
        view_count = int(db.query("SELECT COUNT(*) AS c FROM sys.views")[0]["c"])
        proc_count = int(db.query("SELECT COUNT(*) AS c FROM sys.procedures")[0]["c"])
        index_count = int(db.query("SELECT COUNT(*) AS c FROM sys.indexes WHERE object_id > 0")[0]["c"])

    return {
        "database_name": getattr(db, "database_name", db.dialect),
        "database_size_bytes": 0,
        "table_count": len(tables),
        "view_count": view_count,
        "proc_count": proc_count,
        "index_count": index_count,
        "total_rows": total_rows,
        "largest_table": largest[0],
        "largest_table_rows": largest[1],
        "smallest_table": smallest[0],
        "smallest_table_rows": smallest[1],
        "creation_date": "N/A",
        "compatibility_level": "N/A",
        "recovery_model": "N/A",
    }


def inspect_database_details(db: Database) -> list[dict[str, object]]:
    """Return a detailed, table-oriented inspection payload for reporting."""
    details = []
    for table_name in list_tables(db):
        table = describe_table(db, table_name)
        details.append(
            {
                "name": table.name,
                "row_count": table.row_count,
                "columns": [
                    {
                        "name": column.name,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "is_temporal": column.is_temporal,
                    }
                    for column in table.columns
                ],
            }
        )
    return details
