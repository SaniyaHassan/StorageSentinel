from dbarchive.archive_analyzer import analyze_tables
from dbarchive.models import ColumnInfo, TableInfo


def test_analyze_tables_identifies_timestamp_candidates_and_skips_static_tables():
    tables = [
        TableInfo(
            name="dbo.Orders",
            columns=[
                ColumnInfo("OrderID", "int", False, False),
                ColumnInfo("CreatedDate", "datetime", False, True),
            ],
            row_count=100,
        ),
        TableInfo(
            name="dbo.Users",
            columns=[
                ColumnInfo("UserID", "int", False, False),
                ColumnInfo("Name", "nvarchar(100)", False, False),
            ],
            row_count=50,
        ),
        TableInfo(
            name="dbo.Logs",
            columns=[
                ColumnInfo("LogID", "int", False, False),
                ColumnInfo("Timestamp", "datetime", False, True),
            ],
            row_count=250,
        ),
    ]

    report = analyze_tables(tables)

    eligible = [item for item in report if item.status == "eligible"]
    skipped = [item for item in report if item.status == "skipped"]

    assert any(item.name == "dbo.Orders" and item.archive_column == "CreatedDate" for item in eligible)
    assert any(item.name == "dbo.Users" and "No timestamp" in item.reason for item in skipped)
    assert any(item.name == "dbo.Logs" and item.archive_column == "Timestamp" for item in eligible)


def test_business_date_never_beats_a_lifecycle_date():
    # A table with both a business date (OrderDate) and a lifecycle date
    # (CreatedDate) must archive on CreatedDate, never OrderDate — archiving
    # on OrderDate would sweep out old orders regardless of when the row was
    # actually written, which is a different (and wrong) kind of archival.
    table = TableInfo(
        name="dbo.Orders",
        columns=[
            ColumnInfo("OrderID", "int", False, False),
            ColumnInfo("OrderDate", "datetime", False, True),
            ColumnInfo("CreatedDate", "datetime", False, True),
        ],
        row_count=100,
    )
    report = analyze_tables([table])
    assert report[0].status == "eligible"
    assert report[0].archive_column == "CreatedDate"


def test_restore_temp_tables_are_excluded_from_analysis():
    # restorer.py's temp-table mode creates "<table>_restore_<8 hex chars>",
    # mirroring the source schema including its timestamp column. It must
    # never be picked back up as a fresh archive candidate.
    table = TableInfo(
        name="patients_restore_a1b2c3d4",
        columns=[ColumnInfo("created_date", "datetime", False, True)],
        row_count=50,
    )
    report = analyze_tables([table])
    assert report[0].status == "skipped"
    assert "restore" in report[0].reason.lower()


def test_archive_suffixed_tables_are_excluded_from_analysis():
    table = TableInfo(
        name="dbo.Orders_archive",
        columns=[ColumnInfo("CreatedDate", "datetime", False, True)],
        row_count=50,
    )
    report = analyze_tables([table])
    assert report[0].status == "skipped"
