"""Interactive CLI menu and command helpers for the archival service."""

from __future__ import annotations

from pathlib import Path

from . import ArchiveError, catalog as catalog_mod
from .archive_analyzer import analyze_database
from .archiver import run_archive_all
from .config import Config
from .database import Database, make_database
from .inspector import inspect_database, inspect_database_details
from .logger import configure, get_logger
from .report_generator import ReportGenerator
from .restorer import restore, verify
from .utils import color_enabled

log = get_logger()


class CliApp:
    """Simple interactive CLI shell that exposes the main workflows."""

    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.reporter = ReportGenerator(self.cfg.get_path("reports_location"))

    def run(self) -> int:
        while True:
            print("\n==================================================")
            print("DATABASE ARCHIVAL SERVICE")
            print("==================================================")
            print("1. Database Summary Report")
            print("2. Detailed Database Report")
            print("3. Analyze Database for Archival")
            print("4. Export and Archive Database")
            print("5. View Archive History")
            print("6. Restore Archived Data")
            print("7. Configuration")
            print("8. Help")
            print("9. Exit")
            try:
                choice = input("Select an option: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if choice == "1":
                self.show_summary_report()
            elif choice == "2":
                self.show_detailed_report()
            elif choice == "3":
                self.analyze_archival_candidates()
            elif choice == "4":
                self.run_archive_workflow()
            elif choice == "5":
                self.show_history()
            elif choice == "6":
                self.restore_workflow()
            elif choice == "7":
                self.show_configuration()
            elif choice == "8":
                self.show_help()
            elif choice == "9":
                return 0
            else:
                print("Invalid choice.")

    def show_summary_report(self) -> None:
        summary = inspect_database(self.db)
        print("Database summary")
        print("=" * 24)
        for key, value in summary.items():
            print(f"{key}: {value}")
        self.reporter.write_json("database_summary", summary)

    def show_detailed_report(self) -> None:
        details = inspect_database_details(self.db)
        print("Detailed database report")
        print("=" * 24)
        for table in details:
            print(f"- {table['name']} ({table['row_count']} rows)")
            for column in table["columns"]:
                print(
                    f"  • {column['name']} [{column['data_type']}] "
                    f"nullable={column['nullable']} temporal={column['is_temporal']}"
                )
        self.reporter.write_json("database_details", details)

    def analyze_archival_candidates(self) -> None:
        report = analyze_database(self.db)
        for item in report:
            print(f"{item.name}: {item.status} -> {item.archive_column or 'n/a'} | {item.reason}")

    def run_archive_workflow(self) -> None:
        threshold = input("Enter archive threshold in days: ").strip() or str(self.cfg.get_int("default_threshold_days"))
        self.cfg._values["archive_age_days"] = int(threshold)
        delete_original = input("Delete source rows after archive? [Y/n]: ").strip().lower()
        keep_source = delete_original in ("n", "no")

        summary = run_archive_all(self.db, self.cfg, keep_source=keep_source, dry_run=self.cfg.get_bool("dry_run"))
        report_text = self._render_run_summary(summary)
        print(report_text)
        self.reporter.write_text("archive_run_summary", report_text)

    @staticmethod
    def _render_run_summary(summary) -> str:
        lines = [f"Processed {summary.processed} tables", ""]

        lines.append("Archived:")
        if summary.archived:
            for rec in summary.archived:
                lines.append(rec.source_table)
        else:
            lines.append("none")
        lines.append("")

        lines.append("Skipped:")
        if summary.skipped:
            for name, reason in summary.skipped:
                lines.append(f"{name} ({reason})")
        else:
            lines.append("none")
        lines.append("")

        lines.append("Failed:")
        if summary.failed:
            for name, reason in summary.failed:
                lines.append(f"{name} ({reason})")
        else:
            lines.append("none")

        return "\n".join(lines)

    def show_history(self) -> None:
        records = catalog_mod.query_catalog(self.db, self.cfg, None)
        print(self.reporter.render_archive_history(records))
        self.reporter.write_text("archive_history", self.reporter.render_archive_history(records))

    def restore_workflow(self) -> None:
        batch_id = input("Enter archive batch id: ").strip()
        mode = input("Restore mode [verify/csv/temp-table/database]: ").strip() or "verify"
        created_date = input("Enter created date to restore (leave blank for all rows): ").strip()
        result = restore(self.db, self.cfg, batch_id, mode, created_date=created_date or None)
        print(f"[{result.status}] {result.detail}")

    def show_configuration(self) -> None:
        print(self.cfg.mask())

    def show_help(self) -> None:
        print("Use the numbered menu to inspect the database, analyze archive candidates, run an archive, view history, or restore data.")


def build_cli_app(cfg: Config, db: Database) -> CliApp:
    return CliApp(cfg, db)


def run_cli(cfg: Config, db: Database) -> int:
    app = build_cli_app(cfg, db)
    return app.run()
