import pytest

from dbarchive.database import make_database
from dbarchive.errors import DatabaseError


def test_factory_returns_sqlite(demo_cfg):
    db = make_database(demo_cfg)
    assert db.dialect == "sqlite"
    db.close()


def test_seed_populates(demo_db):
    assert demo_db.query("SELECT COUNT(*) AS c FROM patients")[0]["c"] >= 200


def test_query_returns_dicts(demo_db):
    row = demo_db.query("SELECT patient_id, full_name FROM patients LIMIT 1")[0]
    assert set(row.keys()) == {"patient_id", "full_name"}


def test_quote_ident_rejects_unsafe(demo_db):
    with pytest.raises(DatabaseError):
        demo_db.quote_ident("a;b")


def test_quote_ident_quotes(demo_db):
    assert demo_db.quote_ident("col") == '"col"'


def test_rollback_undoes_insert(demo_db):
    before = demo_db.query("SELECT COUNT(*) AS c FROM patients")[0]["c"]
    demo_db.begin()
    demo_db.execute(
        "INSERT INTO patients (patient_id, full_name, dob, created_date) "
        "VALUES (99999, 'X', '2000-01-01', '2020-01-01 00:00:00')"
    )
    demo_db.rollback()
    after = demo_db.query("SELECT COUNT(*) AS c FROM patients")[0]["c"]
    assert before == after


def test_unknown_backend_raises(demo_cfg):
    cfg = demo_cfg
    cfg._values["backend"] = "oracle"
    with pytest.raises(DatabaseError):
        make_database(cfg)
