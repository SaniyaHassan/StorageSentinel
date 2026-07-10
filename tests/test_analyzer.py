from dbarchive.analyzer import analyze, classify, score_column
from dbarchive.models import ColumnInfo, TableInfo
from dbarchive.utils import tokenize


def col(name, temporal=True, nullable=False):
    return ColumnInfo(name, "DATETIME" if temporal else "TEXT", nullable, temporal)


def test_classify_tiers():
    assert classify(tokenize("created_date")) == "lifecycle"
    assert classify(tokenize("updated_at")) == "modification"
    assert classify(tokenize("dob")) == "business"
    assert classify(tokenize("order_date")) == "business"
    assert classify(tokenize("timestamp")) == "generic"
    assert classify(tokenize("full_name")) == "none"


def test_patients_chooses_created_over_dob():
    table = TableInfo(
        "patients",
        [col("patient_id", temporal=False), col("dob"), col("created_date")],
        row_count=1,
    )
    assert analyze(table).chosen == "created_date"


def test_business_only_returns_none():
    table = TableInfo("people", [col("id", temporal=False), col("dob")], row_count=1)
    result = analyze(table)
    assert result.chosen is None
    assert "business" in result.explanation.lower()


def test_lifecycle_beats_modification_score():
    created = score_column(col("created_date"))
    updated = score_column(col("updated_date"))
    assert created.score > updated.score


def test_preferred_column_wins():
    table = TableInfo("patients", [col("dob"), col("created_date")], row_count=1)
    result = analyze(table, preferred="dob")
    assert result.chosen == "dob"
    assert "configured" in result.explanation.lower()


def test_mixed_business_lifecycle_demoted():
    # order_created carries both — should score below a clean created_date.
    clean = score_column(col("created_date"))
    mixed = score_column(col("order_created"))
    assert clean.score > mixed.score
