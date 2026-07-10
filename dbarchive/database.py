"""The database abstraction and its two backends.

One ``Database`` interface hides the dialect. Both backends accept ``?``
placeholders and return rows as plain dicts, so every module above this file is
dialect-neutral — it never learns whether it is talking to SQL Server or SQLite.

- ``SqliteDemoDatabase`` needs nothing beyond the standard library and can seed a
  realistic hospital schema, so the whole pipeline runs with zero external
  services.
- ``SqlServerDatabase`` imports ``pyodbc`` lazily, so importing this module (and
  running the demo) never requires the ODBC driver to be present.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from .config import Config
from .errors import DatabaseError
from .utils import valid_identifier

_TEMPORAL_HINTS = ("date", "time", "datetime", "timestamp", "smalldatetime")


class Database(ABC):
    """Backend-neutral database handle.

    Implementations translate ``?`` placeholders and dict-row results into
    whatever the underlying driver needs. Identifiers passed to ``quote_ident``
    are validated before quoting — that is the guard that keeps table and column
    names (which cannot be parameterized) from carrying an injection.
    """

    dialect: str = "base"

    @abstractmethod
    def query(self, sql: str, params: Sequence = ()) -> list[dict]:
        ...

    @abstractmethod
    def execute(self, sql: str, params: Sequence = ()) -> int:
        ...

    @abstractmethod
    def executemany(self, sql: str, seq: Sequence[Sequence]) -> int:
        ...

    @abstractmethod
    def begin(self) -> None:
        ...

    @abstractmethod
    def commit(self) -> None:
        ...

    @abstractmethod
    def rollback(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    def quote_ident(self, name: str) -> str:
        if not valid_identifier(name):
            raise DatabaseError(
                f"Refusing to use unsafe SQL identifier: {name!r}. "
                f"Identifiers must match a letter/underscore followed by "
                f"letters, digits, or underscores."
            )
        if self.dialect == "sqlserver":
            return f"[{name}]"
        return f'"{name}"'

    def qualify(self, table: str) -> str:
        """Quote a possibly schema-qualified table name (schema.table)."""
        parts = table.split(".")
        return ".".join(self.quote_ident(p) for p in parts)

    @staticmethod
    def placeholder_row(n: int) -> str:
        return "(" + ", ".join(["?"] * n) + ")"


class SqliteDemoDatabase(Database):
    """Offline demo backend on SQLite. Seeds a hospital-style schema."""

    dialect = "sqlite"

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        try:
            self._conn = sqlite3.connect(self._path)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not open SQLite database {self._path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def query(self, sql: str, params: Sequence = ()) -> list[dict]:
        cur = self._conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Sequence = ()) -> int:
        cur = self._conn.execute(sql, tuple(params))
        count = cur.rowcount
        cur.close()
        return count

    def executemany(self, sql: str, seq: Sequence[Sequence]) -> int:
        cur = self._conn.executemany(sql, [tuple(r) for r in seq])
        count = cur.rowcount
        cur.close()
        return count

    def begin(self) -> None:
        # sqlite3 opens an implicit transaction on the first DML; make it explicit
        # so begin/commit/rollback read the same across both backends.
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def seed_demo(self, patients: int = 240) -> None:
        """Create and populate the demo schema.

        The ``patients`` table deliberately carries both a business date (``dob``)
        and a lifecycle date (``created_date``) so the analyzer has a real choice
        to get right. ``created_date`` values span from well over a year ago up to
        today, so age-based archival always has candidate rows.
        """
        self._conn.executescript(
            """
            DROP TABLE IF EXISTS visits;
            DROP TABLE IF EXISTS patients;
            CREATE TABLE patients (
                patient_id   INTEGER PRIMARY KEY,
                full_name    TEXT NOT NULL,
                dob          DATE,
                created_date DATETIME NOT NULL,
                updated_date DATETIME,
                diagnosis    TEXT
            );
            CREATE TABLE visits (
                visit_id   INTEGER PRIMARY KEY,
                patient_id INTEGER NOT NULL,
                visit_date DATETIME NOT NULL,
                notes      TEXT
            );
            """
        )
        base = datetime.now()
        diagnoses = ["Hypertension", "Type 2 Diabetes", "Asthma", "Migraine", "Anemia"]
        rows = []
        for i in range(1, patients + 1):
            # Spread created_date from ~600 days ago to today.
            age_days = (i * 600) // patients
            created = base - timedelta(days=age_days, hours=i % 24)
            dob = base - timedelta(days=365 * 20 + i * 37)
            rows.append(
                (
                    i,
                    f"Patient {i:04d}",
                    dob.date().isoformat(),
                    created.replace(microsecond=0).isoformat(sep=" "),
                    created.replace(microsecond=0).isoformat(sep=" "),
                    diagnoses[i % len(diagnoses)],
                )
            )
        self._conn.executemany(
            "INSERT INTO patients "
            "(patient_id, full_name, dob, created_date, updated_date, diagnosis) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        visit_rows = []
        vid = 1
        for i in range(1, patients + 1):
            for _ in range(i % 3):
                visit_day = base - timedelta(days=(i * 7) % 500)
                visit_rows.append(
                    (vid, i, visit_day.replace(microsecond=0).isoformat(sep=" "), "routine")
                )
                vid += 1
        self._conn.executemany(
            "INSERT INTO visits (visit_id, patient_id, visit_date, notes) VALUES (?, ?, ?, ?)",
            visit_rows,
        )
        self._conn.commit()


class SqlServerDatabase(Database):
    """Production backend over SQL Server via pyodbc (imported lazily)."""

    dialect = "sqlserver"

    def __init__(self, cfg: Config) -> None:
        try:
            import pyodbc  # noqa: PLC0415  (lazy: absent driver must not break the demo)
        except ImportError as exc:
            raise DatabaseError(
                "The SQL Server backend needs pyodbc. Install it with "
                "'pip install pyodbc' and an ODBC driver, or use --backend demo."
            ) from exc

        driver = cfg.get_str("driver") or "ODBC Driver 18 for SQL Server"
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={cfg.get_str('server')}",
            f"DATABASE={cfg.get_str('database')}",
        ]
        user = cfg.get_str("user")
        if user:
            parts.append(f"UID={user}")
            parts.append(f"PWD={cfg.get_str('password')}")
        else:
            parts.append("Trusted_Connection=yes")
        parts.append("TrustServerCertificate=yes")
        conn_str = ";".join(parts)
        try:
            self._conn = pyodbc.connect(conn_str, autocommit=False)
        except pyodbc.Error as exc:  # pragma: no cover - needs a live server
            raise DatabaseError(f"Could not connect to SQL Server: {exc}") from exc

    def _rows_as_dicts(self, cursor) -> list[dict]:
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def query(self, sql: str, params: Sequence = ()) -> list[dict]:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        rows = self._rows_as_dicts(cur)
        cur.close()
        return rows

    def execute(self, sql: str, params: Sequence = ()) -> int:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        count = cur.rowcount
        cur.close()
        return count

    def executemany(self, sql: str, seq: Sequence[Sequence]) -> int:  # pragma: no cover
        cur = self._conn.cursor()
        cur.fast_executemany = True
        cur.executemany(sql, [tuple(r) for r in seq])
        count = cur.rowcount
        cur.close()
        return count

    def begin(self) -> None:  # pragma: no cover
        pass  # pyodbc is already in a transaction under autocommit=False

    def commit(self) -> None:  # pragma: no cover
        self._conn.commit()

    def rollback(self) -> None:  # pragma: no cover
        self._conn.rollback()

    def close(self) -> None:  # pragma: no cover
        self._conn.close()


def make_database(cfg: Config) -> Database:
    """Return the backend named by ``cfg['backend']``."""
    backend = cfg.get_str("backend")
    if backend == "demo":
        return SqliteDemoDatabase(cfg.get_str("database"))
    if backend == "sqlserver":
        return SqlServerDatabase(cfg)
    raise DatabaseError(
        f"Unknown backend {backend!r}. Use 'demo' (SQLite) or 'sqlserver'."
    )


def is_temporal_type(data_type: str) -> bool:
    lowered = data_type.lower()
    return any(hint in lowered for hint in _TEMPORAL_HINTS)
