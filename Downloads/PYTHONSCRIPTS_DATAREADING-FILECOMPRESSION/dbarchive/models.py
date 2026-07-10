"""Domain models — the shared contract between every layer.

The inspector, analyzer, exporter, archiver, and reporter all speak in these
dataclasses rather than in dialect-specific rows or ad-hoc dicts. That is what
lets a SQL Server backend and a SQLite backend produce identical shapes and
lets each module be understood on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ColumnInfo:
    """One column of a table, normalized across dialects."""

    name: str
    data_type: str
    nullable: bool
    is_temporal: bool


@dataclass(frozen=True)
class TableInfo:
    """A table's structure plus its current row count."""

    name: str
    columns: list[ColumnInfo]
    row_count: int

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def get(self, name: str) -> ColumnInfo | None:
        """Return the column with this name, or None. Case-insensitive."""
        lowered = name.lower()
        for column in self.columns:
            if column.name.lower() == lowered:
                return column
        return None


@dataclass(frozen=True)
class CandidateScore:
    """One column's suitability as the archive (record-age) column."""

    column: str
    tier: str
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisResult:
    """The analyzer's verdict: the chosen column, the field of candidates, why."""

    chosen: str | None
    candidates: list[CandidateScore]
    explanation: str


@dataclass(frozen=True)
class CompressionResult:
    """The outcome of compressing one CSV into one gzip file."""

    csv_bytes: int
    gz_bytes: int
    ratio: float
    checksum: str

    @property
    def saved_bytes(self) -> int:
        return max(self.csv_bytes - self.gz_bytes, 0)


@dataclass
class ArchiveRecord:
    """A single archive: its identity, provenance, sizes, and lifecycle status.

    Mutable because ``status`` and ``notes`` change over an archive's life
    (ACTIVE -> RESTORED, or ACTIVE -> CORRUPTED). ``to_row``/``from_row`` are the
    bridge to and from the catalog table.
    """

    batch_id: str
    source_table: str
    archive_table: str
    filename: str
    path: str
    start_ts: str | None
    end_ts: str | None
    rows: int
    csv_bytes: int
    gz_bytes: int
    ratio: float
    checksum: str
    created_at: str
    status: str = "ACTIVE"
    notes: str | None = None

    def to_row(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "source_table": self.source_table,
            "archive_table": self.archive_table,
            "filename": self.filename,
            "path": self.path,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "rows": self.rows,
            "csv_bytes": self.csv_bytes,
            "gz_bytes": self.gz_bytes,
            "ratio": self.ratio,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_row(cls, row: dict) -> "ArchiveRecord":
        return cls(
            batch_id=row["batch_id"],
            source_table=row["source_table"],
            archive_table=row["archive_table"],
            filename=row["filename"],
            path=row["path"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            rows=int(row["rows"]),
            csv_bytes=int(row["csv_bytes"]),
            gz_bytes=int(row["gz_bytes"]),
            ratio=float(row["ratio"]),
            checksum=row["checksum"],
            created_at=row["created_at"],
            status=row["status"],
            notes=row["notes"],
        )


@dataclass
class ArchiveRunSummary:
    """The outcome of one 'Export and Archive Database' run across every table.

    ``skipped`` and ``failed`` are (table_name, reason) pairs rather than bare
    strings, so the CLI can render *why* without re-deriving it. A table lands
    in exactly one of the three buckets, so ``processed`` is just their sum.
    """

    archived: list[ArchiveRecord] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.archived) + len(self.skipped) + len(self.failed)


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of a restore in any of the four modes."""

    mode: str
    status: str
    rows: int
    detail: str


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (shared timestamp format)."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
