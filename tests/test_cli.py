import json

import app
from dbarchive.cli import CliApp
from dbarchive.config import Config
from dbarchive.database import make_database
from dbarchive.models import ArchiveRecord, ArchiveRunSummary


def _cfg_file(tmp_path):
    cfg = {
        "backend": "demo",
        "database": str(tmp_path / "demo.sqlite"),
        "archive_age_days": 200,
        "archive_location": str(tmp_path / "archives"),
        "exports_location": str(tmp_path / "exports"),
        "reports_location": str(tmp_path / "reports"),
        "log_dir": str(tmp_path / "logs"),
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def test_menu_launches_by_default(tmp_path, monkeypatch):
    cfg = _cfg_file(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "9")
    assert app.main(["--config", cfg]) == 0


def test_cli_helpers_work_for_demo_backend(tmp_path, capsys):
    cfg_path = _cfg_file(tmp_path)
    cfg = Config.from_file(cfg_path)
    db = make_database(cfg)
    try:
        db.seed_demo()  # type: ignore[attr-defined]
        cli = CliApp(cfg, db)
        cli.show_summary_report()
        cli.analyze_archival_candidates()
    finally:
        db.close()

    out = capsys.readouterr().out
    assert "Database summary" in out
    assert "patients" in out or "created_date" in out


def test_archive_prompt_allows_keep_source(tmp_path, monkeypatch, capsys):
    cfg_path = _cfg_file(tmp_path)
    cfg = Config.from_file(cfg_path)
    db = make_database(cfg)
    try:
        db.seed_demo()  # type: ignore[attr-defined]
        cli = CliApp(cfg, db)
        inputs = iter(["30", "n"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        called = {}

        def fake_run_archive_all(db_arg, cfg_arg, keep_source, dry_run):
            called["keep_source"] = keep_source
            rec = ArchiveRecord(
                batch_id="batch123", source_table="patients", archive_table="patients_archive",
                filename="a.csv.gz", path="/tmp/a.csv.gz", start_ts="s", end_ts="e",
                rows=5, csv_bytes=100, gz_bytes=30, ratio=70.0, checksum="abc",
                created_at="2026-07-08", status="ACTIVE", notes=None,
            )
            return ArchiveRunSummary(archived=[rec], skipped=[("visits", "no rows older than threshold")])

        monkeypatch.setattr("dbarchive.cli.run_archive_all", fake_run_archive_all)
        cli.run_archive_workflow()
    finally:
        db.close()

    out = capsys.readouterr().out
    assert called["keep_source"] is True
    assert "Processed 2 tables" in out
    assert "Archived:" in out and "patients" in out
    assert "Skipped:" in out and "visits (no rows older than threshold)" in out
    assert "Failed:" in out and "none" in out
