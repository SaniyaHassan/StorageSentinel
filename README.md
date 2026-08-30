# StorageSentinel - Storage Lifecycle Manager

StorageSentinel is a shell-first storage manager for Linux servers. It scans file system usage, classifies large and obsolete data, detects duplicate content, and produces safe cleanup recommendations. The system is designed to preserve protected paths and only execute approved cleanup actions.

## Architecture

- `sentinel.sh` — primary shell scanner and cleanup workflow
- `config.conf` — scan thresholds, protected paths, quotas, and email settings
- `pending_actions.csv` — generated recommendation queue for review and execution
- `server_health.sh` — collects server health metrics into `server_health.db`
- `server_report.sh` — generates server health reports from SQLite data
- `legacy_python/` — legacy Python reference code; not required for shell-only operation

## What’s Final

This repository is finalized around shell automation. The core workflow is implemented in bash, and Python is no longer required for the scanner/report flow.

## Requirements

- Linux with `bash`
- `find`, `du`, `stat`, `df`, `awk`, `sed`, `ps`
- `sqlite3` for server health reporting
- `mail` or `mailx` if email alerting is enabled

## Setup

1. Make the shell scripts executable:
   ```bash
   chmod +x sentinel.sh server_health.sh server_report.sh
   ```

2. Review and customize `config.conf`.

## Usage

StorageSentinel has two independent workflows: a read-only **report** for visibility, and a **scan → approve → clean** cycle for actually reclaiming space.

### Storage Report (read-only)

```bash
./sentinel.sh report [root]
```
- Prints a live system health snapshot (CPU, load, RAM, swap, uptime) plus a detailed storage report: disk utilization, per-user usage vs. quota, file type breakdown, large files, and duplicate file groups.
- Does **not** write `pending_actions.csv` or `history.log` — safe to run anytime, as often as you like, with no side effects.

### Scan & Cleanup Workflow

```bash
./sentinel.sh scan [root]
```
- Scans the filesystem and generates cleanup recommendations (trash, package caches, journald logs, duplicate files, cold directories).
- Prints a cleanup-candidates summary.
- Writes `pending_actions.csv` with the proposed actions and logs the run to `history.log`.

```bash
./sentinel.sh approve
```
- Review and approve or reject recommended actions interactively.

```bash
./sentinel.sh clean
```
- Executes approved cleanup actions.

```bash
./sentinel.sh history
```
- Displays scan history.

### Server Health Monitoring

```bash
./server_health.sh init
```
- Initialize the server health database.

```bash
./server_health.sh collect
```
- Capture one server health sample.

```bash
./server_report.sh daily|weekly|monthly
```
- Generate a server health report from stored samples.

## Configuration

Edit `config.conf` to tune scanner behavior and alerts.

Example settings:

```bash
SCAN_ROOT="/home"
ALERT_WARNING_PCT=80
ALERT_CRITICAL_PCT=90
ALERT_EMERGENCY_PCT=95
LARGE_FILE_GB=5
MIN_DIR_SIZE_GB=1
TRASH_MAX_AGE_DAYS=30
CLEAN_CONDA_CACHE=1
CLEAN_PIP_CACHE=1
CLEAN_JOURNALD_LOGS=1
JOURNALD_MAX_AGE_DAYS=30
DUPLICATE_MIN_SIZE_MB=50
COLD_DATA_DAYS=180
DEFAULT_QUOTA_GB=150.0

PROTECTED_PATHS=("/var/lib/postgresql" "/var/lib/mysql" "/etc" "/boot")
PROTECTED_EXTENSIONS=(".db" ".sqlite" ".sqlite3")

ENABLE_EMAIL=0
MAIL_CMD="mail"
SMTP_SERVER="localhost"
SMTP_PORT=25
FROM_ADDRESS="sentinel@yourdomain.com"
TO_ADDRESSES=("admin@yourdomain.com")

SERVER_HEALTH_DB="server_health.db"
SERVER_HEALTH_ROOT="/"
```

## Report Sections

`./sentinel.sh report` prints:

1. **System Health Snapshot** — live CPU usage, load average, RAM/swap usage, and uptime (read directly from `/proc`, no database required)
2. **Filesystem Utilization** — disk usage, available space, and alert status
3. **User Storage & Quotas** — user usage versus quota and status
4. **File Type Analytics** — category sizes for trash, caches, videos, ISOs, AI models, databases, datasets, and other files
5. **Large Files** (above `LARGE_FILE_GB`) — top large files by owner and path
6. **Duplicate File Summary** — duplicate sets and potential savings

`./sentinel.sh scan` prints a separate **Cleanup Candidates** summary (auto-clean and manual-approval actions) and writes them to `pending_actions.csv`.

## Final Notes

- `sentinel.sh` is the finalized shell-based scanner.
- `legacy_python/` remains for reference only.
- The shell workflow is complete and validated.

## Automation Example

Example cron entries:

```cron
0 2 * * * cd /path/to/StorageSentinel && ./sentinel.sh scan >> /var/log/sentinel_scan.log 2>&1
0 3 * * * cd /path/to/StorageSentinel && ./sentinel.sh clean --auto-only >> /var/log/sentinel_clean.log 2>&1
```