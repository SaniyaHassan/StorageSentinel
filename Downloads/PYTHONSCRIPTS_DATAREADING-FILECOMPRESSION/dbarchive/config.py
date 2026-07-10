"""Layered configuration."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigError

DEFAULTS: dict = {
    "backend": "demo",
    "driver": "",
    "server": "",
    "database": "demo.sqlite",
    "user": "",
    "password": "",
    "archive_age_days": 30,
    "export_size_limit_mb": 100,
    "archive_location": "archives",
    "exports_location": "exports",
    "reports_location": "reports",
    "archive_history_table": "archive_history",
    "catalog_table": "archive_catalog",
    "compression_logs_table": "compression_logs",
    "compression_type": "gzip",
    "export_format": "csv",
    "default_threshold_days": 30,
    "log_dir": "logs",
    "dry_run": False,
}

# NOTE: There is deliberately no "source_table" or "timestamp_column" key
# anywhere in this file. The application now discovers every archivable table
# and its best timestamp column automatically (see archive_analyzer.py and
# analyzer.py) — a fixed, single configured table/column no longer exists as
# a concept in this codebase.

_EXAMPLE = """{
  \"backend\": \"demo\",
  \"database\": \"demo.sqlite\",
  \"archive_age_days\": 30,
  \"export_size_limit_mb\": 100,
  \"archive_location\": \"archives\",
  \"exports_location\": \"exports\",
  \"reports_location\": \"reports\",
  \"archive_history_table\": \"archive_history\",
  \"catalog_table\": \"archive_catalog\",
  \"compression_logs_table\": \"compression_logs\",
  \"compression_type\": \"gzip\",
  \"export_format\": \"csv\",
  \"default_threshold_days\": 30,
  \"dry_run\": false
}
"""

_SQLSERVER_EXAMPLE = """{
  \"backend\": \"sqlserver\",
  \"driver\": \"ODBC Driver 18 for SQL Server\",
  \"server\": \"localhost,1433\",
  \"database\": \"ArchiveTest\",
  \"user\": \"sa\",
  \"password\": \"REPLACE_ME\",
  \"archive_location\": \"archives\",
  \"exports_location\": \"exports\",
  \"reports_location\": \"reports\",
  \"archive_history_table\": \"ArchiveHistory\",
  \"catalog_table\": \"ArchiveCatalog\",
  \"compression_logs_table\": \"CompressionLogs\",
  \"compression_type\": \"gzip\",
  \"export_format\": \"csv\",
  \"default_threshold_days\": 30,
  \"log_dir\": \"logs\",
  \"dry_run\": false
}
"""


class Config:
    """Read-only view over merged configuration with typed accessors."""

    def __init__(self, values: dict) -> None:
        self._values = values

    @classmethod
    def from_file(cls, path: str | Path, overrides: dict | None = None) -> "Config":
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"Config file not found: {config_path}.")
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                file_values = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file {config_path} is not valid JSON: {exc}") from exc
        if not isinstance(file_values, dict):
            raise ConfigError(f"Config file {config_path} must contain a JSON object.")

        merged = dict(DEFAULTS)
        merged.update(file_values)
        if overrides:
            merged.update({k: v for k, v in overrides.items() if v is not None})
        return cls(merged)

    @classmethod
    def from_defaults(cls, overrides: dict | None = None) -> "Config":
        merged = dict(DEFAULTS)
        if overrides:
            merged.update({k: v for k, v in overrides.items() if v is not None})
        return cls(merged)

    def get(self, key: str) -> object:
        if key not in self._values:
            raise ConfigError(f"Unknown config key: {key}")
        return self._values[key]

    def get_str(self, key: str) -> str:
        return str(self.get(key))

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_float(self, key: str) -> float:
        return float(self.get(key))

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def get_path(self, key: str) -> Path:
        return Path(str(self.get(key)))

    def as_dict(self) -> dict:
        return dict(self._values)

    def mask(self) -> dict:
        masked = dict(self._values)
        masked["password"] = "***" if masked.get("password") else ""
        return masked

    @staticmethod
    def write_example(path: str | Path, backend: str = "demo") -> None:
        content = _SQLSERVER_EXAMPLE if backend == "sqlserver" else _EXAMPLE
        Path(path).write_text(content, encoding="utf-8")
