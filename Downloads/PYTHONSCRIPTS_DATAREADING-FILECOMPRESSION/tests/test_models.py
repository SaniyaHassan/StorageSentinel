from dbarchive.models import ArchiveRecord, ColumnInfo, CompressionResult, TableInfo


def _table():
    return TableInfo(
        name="patients",
        columns=[
            ColumnInfo("patient_id", "INTEGER", False, False),
            ColumnInfo("created_date", "DATETIME", False, True),
        ],
        row_count=10,
    )


def test_column_names():
    assert _table().column_names == ["patient_id", "created_date"]


def test_get_column_case_insensitive():
    assert _table().get("CREATED_DATE").name == "created_date"
    assert _table().get("missing") is None


def test_compression_saved_bytes():
    assert CompressionResult(100, 30, 70.0, "x").saved_bytes == 70


def test_archive_record_roundtrip():
    rec = ArchiveRecord(
        batch_id="b1", source_table="patients", archive_table="patients_archive",
        filename="a.csv.gz", path="/tmp/a.csv.gz", start_ts="s", end_ts="e",
        rows=5, csv_bytes=100, gz_bytes=30, ratio=70.0, checksum="abc",
        created_at="2026-07-08", status="ACTIVE", notes=None,
    )
    assert ArchiveRecord.from_row(rec.to_row()) == rec
