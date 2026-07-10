"""The archive pipeline: analyze, export, compress, catalog, prune.

Everything below runs inside a single transaction *per table*. The order
matters: the source rows are deleted only *after* the compressed archive is
written, checksummed, and recorded, so a failure anywhere rolls that table's
work back and no data is lost. A failure on one table never aborts the run —
``run_archive_all`` catches it, records it, and moves on to the next table.

There is no configured "source table" anywhere in this module. Every table
this module touches is discovered by ``archive_analyzer.analyze_database`` and
passed in explicitly by ``run_archive_all``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import catalog
from .archive_analyzer import analyze_database
from .compressor import compress_csv
from .config import Config
from .database import Database
from .errors import ArchivingError
from .exporter import rows_to_csv
from .logger import get_logger
from .models import ArchiveRecord, ArchiveRunSummary, now_iso

log = get_logger()


def build_batch_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:16]}"


def _system_table_names(cfg: Config) -> set[str]:
    """Bare (schema-stripped, lowercased) names of the app's own audit tables.

    These must never be treated as archive candidates -- once they have rows,
    the analyzer would happily "detect" their own created_at/timestamp column
    and the app would start archiving its own history/catalog/logs.
    """
    keys = ("archive_history_table", "catalog_table", "compression_logs_table")
    return {cfg.get_str(key).split(".")[-1].lower() for key in keys}


def run_archive_table(
    db: Database,
    cfg: Config,
    table_name: str,
    column: str,
    *,
    keep_source: bool = False,
    dry_run: bool = False,
) -> ArchiveRecord | None:
    """Archive rows of *table_name* older than the configured age, on *column*.

    Returns the record, or None when nothing qualifies. In ``dry_run`` mode
    nothing is written and the returned record carries ``status='DRY_RUN'``
    with the candidate count.

    Both *table_name* and *column* are supplied by the caller (normally the
    per-table result of ``archive_analyzer.analyze_database``) -- this function
    itself never reads a table or column name out of config.
    """
    quoted_table = db.qualify(table_name)
    quoted_col = db.quote_ident(column)

    cutoff = datetime.now() - timedelta(days=cfg.get_float("archive_age_days"))
    cutoff_str = cutoff.replace(microsecond=0).isoformat(sep=" ")

    where = f"{quoted_col} < ?"
    count = int(
        db.query(f"SELECT COUNT(*) AS c FROM {quoted_table} WHERE {where}", (cutoff_str,))[0]["c"]
    )
    log.info("Archive candidates in %s older than %s: %d", table_name, cutoff_str, count)

    if count == 0:
        return None

    span = db.query(
        f"SELECT MIN({quoted_col}) AS lo, MAX({quoted_col}) AS hi "
        f"FROM {quoted_table} WHERE {where}",
        (cutoff_str,),
    )[0]
    start_ts, end_ts = str(span["lo"]), str(span["hi"])

    batch_id = build_batch_id()
    archive_table = f"{table_name.split('.')[-1]}_archive"
    filename = f"archive_{table_name.split('.')[-1]}_{batch_id}.csv.gz"
    gz_path = cfg.get_path("archive_location") / filename

    if dry_run:
        return ArchiveRecord(
            batch_id=batch_id, source_table=table_name, archive_table=archive_table,
            filename=filename, path=str(gz_path), start_ts=start_ts, end_ts=end_ts,
            rows=count, csv_bytes=0, gz_bytes=0, ratio=0.0,
            checksum="", created_at=now_iso(), status="DRY_RUN",
            notes="Dry run -- nothing was written.",
        )

    tmp_csv = cfg.get_path("archive_location") / f".{batch_id}.csv"
    limit = cfg.get_int("export_size_limit_mb") * 1024 * 1024

    db.begin()
    try:
        written, _ = rows_to_csv(
            db, table_name, column, where, (cutoff_str,), tmp_csv, limit
        )
        comp = compress_csv(tmp_csv, gz_path)
        tmp_csv.unlink(missing_ok=True)

        rec = ArchiveRecord(
            batch_id=batch_id, source_table=table_name, archive_table=archive_table,
            filename=filename, path=str(gz_path.resolve()), start_ts=start_ts, end_ts=end_ts,
            rows=written, csv_bytes=comp.csv_bytes, gz_bytes=comp.gz_bytes, ratio=comp.ratio,
            checksum=comp.checksum, created_at=now_iso(), status="ACTIVE",
            notes=None if not keep_source else "Source rows retained.",
        )

        catalog.ensure_tables(db, cfg)
        catalog.insert_archive(db, cfg, rec)
        catalog.insert_archive_history(
            db, cfg, rec, threshold_days=cfg.get_float("archive_age_days"), keep_source=keep_source
        )
        catalog.insert_compression_log(db, cfg, rec)

        if not keep_source:
            deleted = db.execute(
                f"DELETE FROM {quoted_table} WHERE {where}", (cutoff_str,)
            )
            log.info("Deleted %d source rows after archiving batch %s", deleted, batch_id)

        db.commit()
        log.info(
            "Archived %d rows from %s to %s (%.2f%% smaller)",
            written, table_name, filename, comp.ratio,
        )
        return rec
    except Exception as exc:
        db.rollback()
        tmp_csv.unlink(missing_ok=True)
        if isinstance(gz_path, Path):
            gz_path.unlink(missing_ok=True)
        log.exception("Archive of %s failed; rolled back", table_name)
        raise ArchivingError(f"Archive of {table_name!r} failed and was rolled back: {exc}") from exc


def run_archive_all(
    db: Database,
    cfg: Config,
    *,
    keep_source: bool = False,
    dry_run: bool = False,
) -> ArchiveRunSummary:
    """Drive the full 'Export and Archive Database' workflow.

    1. Analyze the database to find every eligible table and its timestamp
       column (via ``archive_analyzer.analyze_database``).
    2. For each eligible table: count rows older than the threshold, skip if
       none qualify, otherwise export + compress + catalog (+ optionally
       delete source rows), via ``run_archive_table``.
    3. Keep going even if one table fails -- a failure is recorded and the run
       continues with the next table.

    Tables with no timestamp column, no rows, or that are one of the app's own
    audit tables (history/catalog/compression-log) are skipped, never archived.
    """
    catalog.ensure_tables(db, cfg)
    system_names = _system_table_names(cfg)
    candidates = analyze_database(db)

    summary = ArchiveRunSummary()
    for candidate in candidates:
        bare_name = candidate.name.split(".")[-1].lower()

        if bare_name in system_names:
            summary.skipped.append((candidate.name, "internal archive metadata table"))
            continue

        if candidate.status != "eligible":
            summary.skipped.append((candidate.name, candidate.reason))
            continue

        try:
            rec = run_archive_table(
                db, cfg, candidate.name, candidate.archive_column,
                keep_source=keep_source, dry_run=dry_run,
            )
        except ArchivingError as exc:
            log.error("Skipping %s after failure: %s", candidate.name, exc)
            summary.failed.append((candidate.name, exc.message))
            continue

        if rec is None:
            summary.skipped.append((candidate.name, "no rows older than threshold"))
        else:
            summary.archived.append(rec)

    return summary
