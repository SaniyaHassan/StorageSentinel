import gzip

import pytest

from dbarchive.archiver import run_archive_table
from dbarchive.errors import IntegrityError, RestoreError
from dbarchive.restorer import restore


def _count(db):
    return db.query("SELECT COUNT(*) AS c FROM patients")[0]["c"]


def _archive(demo_db, demo_cfg):
    return run_archive_table(demo_db, demo_cfg, "patients", "created_date", keep_source=False, dry_run=False)


def test_verify_mode(demo_db, demo_cfg):
    rec = _archive(demo_db, demo_cfg)
    result = restore(demo_db, demo_cfg, rec.batch_id, "verify")
    assert result.status == "verified"


def test_csv_mode_writes_file(demo_db, demo_cfg):
    rec = _archive(demo_db, demo_cfg)
    result = restore(demo_db, demo_cfg, rec.batch_id, "csv")
    assert result.status == "restored-csv"
    out = demo_cfg.get_path("exports_location") / f"{rec.batch_id}.csv"
    assert out.exists()


def test_temp_table_mode(demo_db, demo_cfg):
    rec = _archive(demo_db, demo_cfg)
    result = restore(demo_db, demo_cfg, rec.batch_id, "temp-table")
    assert result.status == "restored"
    assert result.rows == rec.rows


def test_database_mode_restores_count(demo_db, demo_cfg):
    before = _count(demo_db)
    rec = _archive(demo_db, demo_cfg)
    assert _count(demo_db) == before - rec.rows
    restore(demo_db, demo_cfg, rec.batch_id, "database")
    assert _count(demo_db) == before


def test_database_mode_can_restore_rows_for_matching_created_date(demo_db, demo_cfg):
    target_date = demo_db.query("SELECT created_date FROM patients ORDER BY created_date LIMIT 1")[0]["created_date"]
    expected_matches = demo_db.query(
        "SELECT COUNT(*) AS c FROM patients WHERE created_date = ?",
        (target_date,),
    )[0]["c"]

    rec = _archive(demo_db, demo_cfg)
    result = restore(demo_db, demo_cfg, rec.batch_id, "database", created_date=target_date)

    assert result.rows == expected_matches
    assert demo_db.query(
        "SELECT COUNT(*) AS c FROM patients WHERE created_date = ?",
        (target_date,),
    )[0]["c"] == expected_matches


def test_corrupted_archive_raises(demo_db, demo_cfg):
    rec = _archive(demo_db, demo_cfg)
    # Corrupt the gz payload so the checksum no longer matches.
    with gzip.open(rec.path, "wb") as fh:
        fh.write(b"garbage,data\n1,2\n")
    with pytest.raises(IntegrityError):
        restore(demo_db, demo_cfg, rec.batch_id, "database")


def test_unknown_mode_raises(demo_db, demo_cfg):
    rec = _archive(demo_db, demo_cfg)
    with pytest.raises(RestoreError):
        restore(demo_db, demo_cfg, rec.batch_id, "bogus")


def test_missing_batch_raises(demo_db, demo_cfg):
    with pytest.raises(RestoreError):
        restore(demo_db, demo_cfg, "does-not-exist", "verify")
