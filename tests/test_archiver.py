from dbarchive import catalog
from dbarchive.archiver import run_archive_all, run_archive_table
from dbarchive.restorer import restore


def _count(db, table):
    return db.query(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]


def test_archive_table_moves_rows(demo_db, demo_cfg):
    before = _count(demo_db, "patients")
    rec = run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=False, dry_run=False)
    assert rec is not None
    assert rec.rows > 0
    assert rec.gz_bytes < rec.csv_bytes
    assert _count(demo_db, "patients") == before - rec.rows
    active = catalog.query_catalog(demo_db, demo_cfg, {"status": "ACTIVE"})
    assert len(active) == 1


def test_dry_run_moves_nothing(demo_db, demo_cfg):
    before = _count(demo_db, "patients")
    rec = run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=False, dry_run=True)
    assert rec is not None
    assert rec.status == "DRY_RUN"
    assert _count(demo_db, "patients") == before


def test_keep_source_retains_rows(demo_db, demo_cfg):
    before = _count(demo_db, "patients")
    rec = run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=True, dry_run=False)
    assert rec is not None
    assert _count(demo_db, "patients") == before


def test_nothing_to_archive_returns_none(demo_db, demo_cfg):
    # A huge age threshold means no row is old enough.
    demo_cfg._values["archive_age_days"] = 100000
    assert run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=False, dry_run=False) is None


def test_run_archive_all_processes_every_eligible_table(demo_db, demo_cfg):
    # The demo schema has two tables: patients (created_date) and visits
    # (visit_date). Both carry a usable timestamp column, so both should be
    # archived automatically with no table name configured anywhere.
    summary = run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)

    archived_tables = {rec.source_table for rec in summary.archived}
    assert archived_tables <= {"patients", "visits"}
    assert archived_tables  # at least one table archived
    assert summary.processed == len(summary.archived) + len(summary.skipped) + len(summary.failed)
    assert summary.failed == []


def test_run_archive_all_skips_when_nothing_qualifies(demo_db, demo_cfg):
    demo_cfg._values["archive_age_days"] = 100000
    summary = run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)
    assert summary.archived == []
    assert any(reason == "no rows older than threshold" for _, reason in summary.skipped)


def test_run_archive_all_never_archives_its_own_audit_tables(demo_db, demo_cfg):
    # Run once so the catalog/history/compression-log tables exist and gain rows.
    run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)
    # Running again must not try to archive archive_catalog/archive_history/
    # compression_logs, even though they now have their own created_at column.
    summary = run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)
    archived_tables = {rec.source_table for rec in summary.archived}
    system_tables = {"archive_catalog", "archive_history", "compression_logs"}
    assert not (archived_tables & system_tables)


def test_restored_temp_table_is_never_reharvested(demo_db, demo_cfg):
    # Archive patients, restore into a temp table, then run the full workflow
    # again. The temp table must show up as skipped, never as archived.
    rec = run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=False, dry_run=False)
    result = restore(demo_db, demo_cfg, rec.batch_id, "temp-table")
    assert result.status == "restored"

    summary = run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)
    archived_tables = {r.source_table for r in summary.archived}
    skipped_tables = {name for name, _ in summary.skipped}

    assert not any(name.startswith("patients_restore_") for name in archived_tables)
    assert any(name.startswith("patients_restore_") for name in skipped_tables)


def test_analysis_runs_exactly_once_per_archive_run(demo_db, demo_cfg, monkeypatch):
    import dbarchive.archiver as archiver_mod

    calls = {"n": 0}
    original = archiver_mod.analyze_database

    def counting_analyze(db_arg):
        calls["n"] += 1
        return original(db_arg)

    monkeypatch.setattr(archiver_mod, "analyze_database", counting_analyze)
    run_archive_all(demo_db, demo_cfg, keep_source=False, dry_run=False)
    assert calls["n"] == 1
