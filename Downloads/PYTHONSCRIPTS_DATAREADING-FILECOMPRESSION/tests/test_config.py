import json

import pytest

from dbarchive.config import Config
from dbarchive.errors import ConfigError


def _write(tmp_path, data):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_file_overrides_default(tmp_path):
    cfg = Config.from_file(_write(tmp_path, {"archive_age_days": 7}))
    assert cfg.get_int("archive_age_days") == 7


def test_override_beats_file(tmp_path):
    cfg = Config.from_file(_write(tmp_path, {"backend": "demo"}), {"backend": "sqlserver"})
    assert cfg.get_str("backend") == "sqlserver"


def test_none_override_ignored(tmp_path):
    cfg = Config.from_file(_write(tmp_path, {"backend": "demo"}), {"backend": None})
    assert cfg.get_str("backend") == "demo"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_file(tmp_path / "nope.json")


def test_bad_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.from_file(p)


def test_mask_redacts_password(tmp_path):
    cfg = Config.from_file(_write(tmp_path, {"password": "secret"}))
    masked = cfg.mask()
    assert masked["password"] == "***"
    assert masked["password"] != "secret"


def test_get_int_coerces(tmp_path):
    cfg = Config.from_file(_write(tmp_path, {"archive_age_days": "5"}))
    assert cfg.get_int("archive_age_days") == 5
