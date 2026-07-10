#!/usr/bin/env python3
"""Entry point for the interactive database archival CLI menu."""

from __future__ import annotations

import argparse
import sys

# The reports use box-drawing characters. On Windows the console often defaults
# to a legacy code page (cp1252) that cannot encode them; switch stdout/stderr to
# UTF-8 up front, falling back to replacement so output never crashes.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

from dbarchive import ArchiveError, __version__
from dbarchive.cli import run_cli
from dbarchive.config import Config
from dbarchive.database import make_database
from dbarchive.logger import configure, get_logger


def build_parser() -> argparse.ArgumentParser:
    globals_parser = argparse.ArgumentParser(add_help=False)
    globals_parser.add_argument("--config", default="sample_config.json", help="Path to JSON config")
    globals_parser.add_argument("--backend", choices=["demo", "sqlserver"], help="Override the backend")
    globals_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    globals_parser.add_argument("--verbose", action="store_true", help="Debug-level logging")

    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Start the interactive archival CLI menu.",
        parents=[globals_parser],
    )
    parser.add_argument("--version", action="version", version=f"dbarchive {__version__}")
    parser.add_argument("command", nargs="?", choices=["menu", "cli"], default="menu", help="Launch the interactive archival menu")
    return parser


def _load(args: argparse.Namespace) -> Config:
    overrides = {}
    if args.backend:
        overrides["backend"] = args.backend
    if args.dry_run:
        overrides["dry_run"] = True
    return Config.from_file(args.config, overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = get_logger()
    try:
        cfg = _load(args)
    except ArchiveError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    configure(cfg.get_path("log_dir"), verbose=args.verbose)
    try:
        db = make_database(cfg)
    except ArchiveError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    try:
        return run_cli(cfg, db)
    except ArchiveError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # unexpected: log the full trace, show a short line
        log.exception("Unexpected failure")
        print(f"Unexpected error: {exc} (see logs)", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
