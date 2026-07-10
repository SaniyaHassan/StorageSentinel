# Database Archival & Compression CLI

A single interactive CLI menu application for database archival, export, compression, cataloging, and restore. The tool runs with a SQLite demo backend by default and can target Microsoft SQL Server using the `sqlserver` backend.

The application is fully automatic: it discovers every archivable table and its best timestamp column itself. There is no "source table" to configure — you point it at a database and it figures out what to archive.

## What the program does

This CLI app provides:

- database summary and detailed schema reports
- automatic archival-candidate analysis across every table in the database, based on detected timestamp columns
- export of rows older than a threshold, for every eligible table in one run
- compression of exported data into `.gz` archive files
- recording archive history, archive catalog, and compression-log metadata
- optional deletion of source rows after archiving
- restoration of archived batches

## Project structure

- `app.py` – interactive CLI entry point
- `dbarchive/` – library package with config, database backends, inspection, analysis, archiving, cataloging, and restore logic
- `sample_config.json` – starter configuration for the demo (SQLite) backend
- `sqlserver_config.json` – starter configuration for the SQL Server backend (e.g. the `ArchiveTest` database)
- `tests/` – pytest regression tests

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the interactive CLI menu:

```bash
python app.py --config sample_config.json
```

If you want to use the demo backend explicitly:

```bash
python app.py --backend demo --config sample_config.json
```

From the menu you can:

- view a database summary report
- view a detailed database report
- analyze every table for archival suitability
- run an export/archive cycle across every eligible table
- review archive history
- restore archived data
- inspect the active configuration

## How table and column discovery works

Choosing menu option `3` ("Analyze Database for Archival") scans every table in the connected database and, for each one:

- inspects its columns and picks the best timestamp/date column using a scoring model (lifecycle columns like `CreatedDate` beat modification columns like `UpdatedDate`, which beat generic date columns, which beat pure business dates like `DueDate` or `ExpiryDate`)
- skips tables with no usable timestamp column
- skips empty tables
- skips the application's own audit tables (`archive_history_table`, `catalog_table`, `compression_logs_table` from config) so the app never tries to archive its own history

Choosing menu option `4` ("Export and Archive Database") reuses this exact same analysis, then loops over every eligible table it finds:

```
Processed 15 tables

Archived:
Customers
Orders
AuditLogs
Documents
SensorData

Skipped:
ArchiveHistory (empty)
SystemSettings (no timestamp)
Categories (no rows older than threshold)

Failed:
none
```

If a table fails partway through (e.g. a locked table, a transient connection error), that failure is recorded and the run continues with the remaining tables — one bad table never stops the whole archive run.

## Option 4 behavior

When you choose `4`, the CLI asks:

- `Enter archive threshold in days:` (applied to every table in this run)
- `Delete source rows after archive? [Y/n]:` (applied to every table in this run)

If you answer `n` or `no`, each table's archive keeps the original rows in that source table.

## Configuration

The configuration file contains **only connection settings and global settings** — there is no table name or column name in it anywhere:

```json
{
  "backend": "sqlserver",
  "driver": "ODBC Driver 18 for SQL Server",
  "server": "localhost,1433",
  "database": "ArchiveTest",
  "user": "sa",
  "password": "Database123!",

  "archive_location": "archives",
  "exports_location": "exports",
  "reports_location": "reports",

  "archive_history_table": "ArchiveHistory",
  "catalog_table": "ArchiveCatalog",
  "compression_logs_table": "CompressionLogs",

  "compression_type": "gzip",
  "export_format": "csv",

  "default_threshold_days": 30,
  "log_dir": "logs",
  "dry_run": false
}
```

Key fields:

- `backend`: `demo` (SQLite) or `sqlserver`
- `driver` / `server` / `database` / `user` / `password`: SQL Server connection details (ignored for the demo backend)
- `archive_location` / `exports_location` / `reports_location` / `log_dir`: output directories
- `archive_history_table` / `catalog_table` / `compression_logs_table`: names of the three audit tables the app creates and writes to on the target database — also the tables the analyzer always excludes from archival
- `default_threshold_days`: the value pre-filled at the "Enter archive threshold in days" prompt
- `dry_run`: when true, the archive workflow reports what it would do without writing anything

This file is used with the SQL Server `ArchiveTest` database:

```bash
python app.py --config sqlserver_config.json
```

## Testing

Run the test suite with:

```bash
python -m pytest tests -q
```

## Notes

- This project is currently a CLI-only application; there is no web server component.
- `sample_config.json` and `sqlserver_config.json` are starting points, not production configurations — replace the credentials before real use.
- The SQL Server backend requires `pyodbc` and the appropriate ODBC driver.
- Each table's archive operation is transactional: if anything fails for that table, the rollback preserves that table's source data. A failure on one table does not affect any other table in the same run.
- The archive table name recorded per batch is `<table>_archive`, created by mirroring the source table's columns.
- Export files are written atomically using a temporary file + rename pattern.
- Do not modify binaries, deployment files, or existing application logs.
- The service is intentionally isolated in this project folder and uses only the configured export/archive directories.
