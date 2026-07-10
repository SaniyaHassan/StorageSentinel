"""Logging setup — a rotating file audit trail plus console output.

Every significant operation logs a start and finish line with timing, so the
``logs/`` directory becomes a record of what the tool did and when. Rotation
caps disk use at 1 MB per file across 3 backups.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "dbarchive"
_configured = False


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def configure(log_dir: Path, verbose: bool = False) -> None:
    """Attach a rotating file handler and a console handler. Idempotent."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    if _configured:
        # Only adjust verbosity on repeat calls; never stack duplicate handlers.
        for handler in logger.handlers:
            handler.setLevel(level)
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "dbarchive.log", maxBytes=1_048_576, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)

    logger.propagate = False
    _configured = True
