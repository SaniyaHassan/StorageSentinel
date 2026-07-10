"""Restore an archive in one of four modes, always checksum-gated.

Before a single byte is written back, the gzip file's checksum is recompared to
the value recorded at archive time. A mismatch marks the catalog row CORRUPTED
and aborts — a damaged archive can never be replayed into a live table.

Modes, from least to most invasive:
- ``verify``      : recompute the checksum only; no writes.
- ``csv``         : decompress into the exports directory.
- ``temp-table``  : load the rows into a fresh ``<source>_restore_<batch>`` table.
- ``database``    : load the rows back into the source table.

The two metadata columns the archive CSV never actually carries here are
tolerated defensively: any column not present on the target table is dropped
before insert, so a restore adapts to schema drift instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

from . import catalog
from .compressor import decompress
from .config import Config
from .database import Database
from .errors import IntegrityError, RestoreError
from .exporter import read_csv
from .inspector import describe_table
from .logger import get_logger
from .models import ArchiveRecord, RestoreResult, now_iso
from .utils import sha256_file

log = get_logger()

_MODES = ("verify", "csv", "temp-table", "database")


def verify(rec: ArchiveRecord) -> bool:
    path = Path(rec.path)
    if not path.exists():
        return False
    return sha256_file(path) == rec.checksum


def _confined_path(base: Path, name: str) -> Path:
    """Resolve *name* under *base*, refusing anything that escapes the directory."""
    base = base.resolve()
    target = (base / name).resolve()
    if base != target and base not in target.parents:
        raise RestoreError(f"Refusing to write outside {base}: {name!r}")
    return target


def _load_rows(rec: ArchiveRecord) -> tuple[list[str], list[list[str]], Path]:
    tmp = Path(rec.path).with_suffix(".restore.csv")
    decompress(Path(rec.path), tmp)
    header, rows = read_csv(tmp)
    return header, rows, tmp


def _insert_into(db: Database, table: str, target_cols: list[str], header: list[str], rows: list[list[str]]) -> int:
    keep = [i for i, name in enumerate(header) if name in set(target_cols)]
    cols = [header[i] for i in keep]
    quoted = db.quote_ident(table)
    col_sql = ", ".join(db.quote_ident(c) for c in cols)
    placeholders = ", ".join(["?"] * len(cols))
    payload = [[row[i] for i in keep] for row in rows]
    if payload:
        db.executemany(
            f"INSERT INTO {quoted} ({col_sql}) VALUES ({placeholders})", payload
        )
    return len(payload)


def restore(db: Database, cfg: Config, batch_id: str, mode: str) -> RestoreResult:
    if mode not in _MODES:
        raise RestoreError(f"Unknown restore mode {mode!r}. Use one of: {', '.join(_MODES)}.")

    rec = catalog.get_by_batch_id(db, cfg, batch_id)
    if rec is None:
        raise RestoreError(f"No archive found with batch id {batch_id!r}.")

    if not Path(rec.path).exists():
        catalog.update_status(db, cfg, batch_id, "DELETED", notes="Archive file missing")
        raise RestoreError(f"Archive file is missing: {rec.path}")

    if not verify(rec):
        catalog.update_status(db, cfg, batch_id, "CORRUPTED", notes="Checksum mismatch")
        raise IntegrityError(
            f"Archive {rec.filename} failed its checksum — the file is corrupted. "
            f"Marked CORRUPTED; not restoring."
        )

    if mode == "verify":
        return RestoreResult(mode=mode, status="verified", rows=rec.rows,
                             detail="Checksum matches the value recorded at archive time.")

    if mode == "csv":
        out = _confined_path(cfg.get_path("exports_location"), f"{rec.batch_id}.csv")
        decompress(Path(rec.path), out)
        log.info("Restored %s to CSV at %s", rec.filename, out)
        return RestoreResult(mode=mode, status="restored-csv", rows=rec.rows,
                             detail=f"Wrote {out}")

    header, rows, tmp = _load_rows(rec)
    try:
        if mode == "temp-table":
            source = describe_table(db, rec.source_table)
            temp_table = f"{rec.source_table.split('.')[-1]}_restore_{rec.batch_id[-8:]}"
            db.execute(f"DROP TABLE IF EXISTS {db.quote_ident(temp_table)}")
            db.execute(
                f"CREATE TABLE {db.quote_ident(temp_table)} AS "
                f"SELECT * FROM {db.qualify(rec.source_table)} WHERE 1 = 0"
            ) if db.dialect == "sqlite" else db.execute(  # pragma: no cover
                f"SELECT * INTO {db.quote_ident(temp_table)} "
                f"FROM {db.qualify(rec.source_table)} WHERE 1 = 0"
            )
            inserted = _insert_into(db, temp_table, source.column_names, header, rows)
            db.commit()
            catalog.update_status(db, cfg, batch_id, "RESTORED",
                                  notes=f"Restored to {temp_table} at {now_iso()}")
            return RestoreResult(mode=mode, status="restored", rows=inserted,
                                 detail=f"Loaded {inserted} rows into {temp_table}.")

        # mode == "database"
        source = describe_table(db, rec.source_table)
        inserted = _insert_into(db, rec.source_table, source.column_names, header, rows)
        db.commit()
        catalog.update_status(db, cfg, batch_id, "RESTORED",
                              notes=f"Restored into source at {now_iso()}")
        return RestoreResult(mode=mode, status="restored", rows=inserted,
                             detail=f"Loaded {inserted} rows into {rec.source_table}.")
    finally:
        tmp.unlink(missing_ok=True)
