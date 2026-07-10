import pytest

from dbarchive.errors import SchemaError
from dbarchive.inspector import describe_table, list_tables


def test_list_tables(demo_db):
    tables = list_tables(demo_db)
    assert "patients" in tables and "visits" in tables


def test_describe_marks_temporal(demo_db):
    info = describe_table(demo_db, "patients")
    assert info.get("created_date").is_temporal
    assert info.get("dob").is_temporal
    assert not info.get("full_name").is_temporal


def test_describe_row_count(demo_db):
    assert describe_table(demo_db, "patients").row_count >= 200


def test_unknown_table_raises(demo_db):
    with pytest.raises(SchemaError):
        describe_table(demo_db, "nonexistent")
