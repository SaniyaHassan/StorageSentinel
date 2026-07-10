"""Modular database archival and compression toolkit.

A dialect-neutral pipeline that exports database rows to CSV, compresses them to
gzip with checksums, catalogs each archive, and restores them on demand — over a
SQL Server backend in production and an offline SQLite demo backend for
evaluation and tests.
"""

from __future__ import annotations

from .errors import (
    AnalysisError,
    ArchiveError,
    ArchivingError,
    ConfigError,
    DatabaseError,
    IntegrityError,
    RestoreError,
    SchemaError,
)

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "ArchiveError",
    "ConfigError",
    "DatabaseError",
    "SchemaError",
    "AnalysisError",
    "ArchivingError",
    "RestoreError",
    "IntegrityError",
]
