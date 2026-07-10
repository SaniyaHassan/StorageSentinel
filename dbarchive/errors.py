"""User-facing exception hierarchy.

Every error raised by the package is an ``ArchiveError`` carrying a message that
tells the user both what failed and what to do about it. The CLI catches this
base class, prints the message, and exits non-zero; anything that is *not* an
``ArchiveError`` is treated as a bug and logged with a full traceback.
"""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for every error this package raises on purpose."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(ArchiveError):
    """Configuration is missing, malformed, or internally inconsistent."""


class DatabaseError(ArchiveError):
    """A backend could not connect, or a driver is unavailable."""


class SchemaError(ArchiveError):
    """A table or column the operation needs does not exist."""


class AnalysisError(ArchiveError):
    """The archive-column analyzer was given something it cannot reason about."""


class ArchivingError(ArchiveError):
    """The archive pipeline could not complete (and was rolled back)."""


class RestoreError(ArchiveError):
    """A restore was requested that cannot be carried out as asked."""


class IntegrityError(ArchiveError):
    """An archive file failed its checksum — the data on disk is not trustworthy."""
