from dbarchive import catalog
from dbarchive.models import ArchiveRecord


def _rec(batch_id="b1", status="ACTIVE"):
    return ArchiveRecord(
        batch_id=batch_id, source_table="patients", archive_table="patients_archive",
        filename=f"{batch_id}.csv.gz", path=f"/tmp/{batch_id}.csv.gz",
        start_ts="2024-01-01", end_ts="2024-06-01", rows=5, csv_bytes=100,
        gz_bytes=30, ratio=70.0, checksum="abc", created_at="2026-07-08 00:00:00",
        status=status, notes=None,
    )


def test_empty_catalog(demo_db, demo_cfg):
    assert catalog.query_catalog(demo_db, demo_cfg) == []


def test_insert_and_get(demo_db, demo_cfg):
    catalog.ensure_tables(demo_db, demo_cfg)
    rec = _rec()
    catalog.insert_archive(demo_db, demo_cfg, rec)
    got = catalog.get_by_batch_id(demo_db, demo_cfg, "b1")
    assert got == rec


def test_status_transition(demo_db, demo_cfg):
    catalog.ensure_tables(demo_db, demo_cfg)
    catalog.insert_archive(demo_db, demo_cfg, _rec())
    catalog.update_status(demo_db, demo_cfg, "b1", "RESTORED")
    restored = catalog.query_catalog(demo_db, demo_cfg, {"status": "RESTORED"})
    assert len(restored) == 1 and restored[0].status == "RESTORED"


def test_filter_unknown_batch(demo_db, demo_cfg):
    catalog.ensure_tables(demo_db, demo_cfg)
    assert catalog.get_by_batch_id(demo_db, demo_cfg, "missing") is None
