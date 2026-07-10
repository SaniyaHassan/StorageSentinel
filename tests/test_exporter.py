from dbarchive.exporter import read_csv, rows_to_csv


def test_export_writes_header_and_rows(tmp_path, demo_db):
    out = tmp_path / "export.csv"
    count, size = rows_to_csv(demo_db, "patients", "created_date", None, (), out, None)
    assert count >= 200
    assert size > 0

    header, rows = read_csv(out)
    assert "patient_id" in header
    assert len(rows) == count


def test_export_with_where(tmp_path, demo_db):
    out = tmp_path / "some.csv"
    count, _ = rows_to_csv(
        demo_db, "patients", "created_date",
        f'{demo_db.quote_ident("patient_id")} <= ?', (5,), out, None,
    )
    assert count == 5
