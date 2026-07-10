"""Shared fixtures — every test runs on a fresh, seeded SQLite demo backend.

No network, no live database, no external services. Each test gets its own
temp-file demo DB plus temp archive/export/log directories, so tests are fully
isolated and deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbarchive.config import Config
from dbarchive.database import SqliteDemoDatabase


@pytest.fixture
def demo_cfg(tmp_path) -> Config:
    return Config.from_defaults(
        {
            "backend": "demo",
            "database": str(tmp_path / "demo.sqlite"),
            "archive_age_days": 200,
            "archive_location": str(tmp_path / "archives"),
            "exports_location": str(tmp_path / "exports"),
            "log_dir": str(tmp_path / "logs"),
        }
    )


@pytest.fixture
def demo_db(demo_cfg) -> SqliteDemoDatabase:
    db = SqliteDemoDatabase(demo_cfg.get_str("database"))
    db.seed_demo()
    yield db
    db.close()
