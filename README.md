# StorageSentinel

## What this does (in simple terms)

StorageSentinel is a command-line Python tool for keeping a Linux server's disk usage under control. It looks at your system's health (CPU, RAM, swap, uptime), tells you which mounted filesystems, top-level folders, and individual files are using the most space, and then — only in a separate, explicit step — goes looking for specific things that are usually safe to delete: files sitting in Trash directories, cache folders, temporary files, old log files, and exact duplicate files. It never deletes anything by itself. Every single candidate it finds has to be reviewed and approved **one at a time** by a human before anything is removed, and certain paths and file extensions (databases, `/etc`, `/boot`, Docker/Postgres/MySQL/SQL Server data, `.bak`/`.sql` files, etc.) can never be deleted no matter what.

The workflow is five simple stages:
1. **`status`** — a quick, instant health check.
2. **`report`** — a read-only storage report. It traverses the filesystem to size folders and find large files, but it skips the expensive duplicate-hashing and cleanup-candidate analysis that `scan` does.
3. **`scan`** — an expensive, deep filesystem walk that finds cleanup candidates and writes them to a file. Nothing is deleted here either.
4. **`pending`** — you go through the candidates one by one and decide yes/no/skip.
5. **`delete`** — only the candidates you explicitly said "yes" to actually get removed.

---

## Quick start: how to run it

### 1. Requirements
- Linux (it reads `/proc/stat`, `/proc/meminfo`, `/proc/uptime`, and shells out to `findmnt`, so it won't work on Windows/macOS)
- Python 3.9+
- The `PyYAML` package

Install the one dependency:
```bash
python3 -m pip install --user pyyaml
```

### 2. Files you need
Keep these in the same folder (the script always looks for `config.yaml` next to itself, not in the current working directory):
- `sentinel.py` — the program
- `config.yaml` — its configuration

### 3. Check server health (instant, always safe)
```bash
./sentinel.py status
```

### 4. Get a storage report (read-only, no cleanup analysis)
```bash
./sentinel.py report
```
Prints a report to the terminal and also saves a copy to `storage_report.txt`. This does **not** perform the expensive duplicate-hashing and cleanup-candidate classification that `scan` does — but it still fully traverses `scan_root` to size top-level folders and find the largest files, so on a large disk it can take well over a minute (not a quick, shallow check).

### 5. Run the full cleanup workflow
```bash
# Step 1: deep scan — finds trash, caches, temp files, old logs, and duplicates
./sentinel.py scan

# Step 2: review each candidate one by one and approve/reject
./sentinel.py pending

# Step 3: delete only what you approved (asks for one final confirmation)
./sentinel.py delete
```

### 6. See what's happened so far
```bash
./sentinel.py history
```

There's no `--config` flag in this version — the config file location is fixed relative to the script (`config.yaml` in the same directory as `sentinel.py`).

---

## Detailed explanation of every part

### A. The configuration file (`config.yaml`)

| Section | Key | Meaning |
|---|---|---|
| `server` | `scan_root` | Top-level directory to scan (`/` by default). |
| | `excluded_filesystems` | A list of filesystem *types* (e.g. `proc`, `sysfs`, `tmpfs`, `overlay`, `cgroup2`) that are skipped when listing real filesystems in the report — these are virtual/kernel filesystems, not real storage. |
| | `excluded_paths` | Specific virtual/transient paths (`/proc`, `/sys`, `/dev`, `/run`, `/snap`) that are never walked into during scanning. |
| | `protected_paths` | Directories that can be reported on but can **never** be deleted from (`/etc`, `/boot`, SQL Server/PostgreSQL/MySQL/Docker/Containerd data directories). |
| `report` | `largest_files` / `largest_folders` | How many entries to list in those sections (20 each by default). |
| | `folder_depth` | Reserved for a "fast folder report" depth limit (present in config; the current top-level folder sizing walks each top-level directory fully rather than using this value to cap recursion). |
| | `minimum_file_size_mb` | Files smaller than this are ignored in the "largest files" report. |
| `scan` | `find_duplicates` / `duplicate_min_size_mb` | Whether to look for duplicate files, and the minimum size (in MB) a file must be to be considered. |
| | `find_trash` | Whether to look for files inside `.local/share/Trash` directories. |
| | `find_caches` | Whether to look for files inside recognized cache directories. |
| | `find_temp_files` | Whether to look for files under `/tmp`, `/var/tmp`, or `.cache/`. |
| | `find_old_logs` / `old_log_days` | Whether to flag `.log` files, and how old (in days) they must be to qualify (30 by default). |
| | `find_cold_data` / `cold_data_days` | Disabled by default (`find_cold_data: false`) and not currently used to generate candidates — see the safety notes at the end of this document. |
| | `max_duplicate_file_gb` | Skip duplicate-hashing files larger than this many GB (`0` = unlimited). |
| | `partial_hash_bytes` | How many bytes to read for the fast first-pass duplicate hash (1 MB by default). |
| `cleanup` | `allow_trash` / `allow_caches` / `allow_temp_files` / `allow_old_logs` / `allow_duplicates` | Per-category on/off switches — a category can be *found* by `scan` settings above but still excluded from candidates if its `allow_*` flag is off. |
| | `protected_extensions` | File extensions that can never be deleted, regardless of category: `.db`, `.sqlite`, `.sqlite3`, `.mdf`, `.ndf`, `.ldf`, `.bak`, `.sql`. |
| `logging` (output/persistence paths, not just log verbosity) | `history_file` / `report_file` / `pending_file` / `log_file` | Filenames (relative to the script's own directory) for the run history, saved report, pending-candidates CSV, and operational log. |

**Safety point:** `protected_paths` and `protected_extensions` are checked when candidates are generated by `scan`, and checked *again* (twice) right before each individual deletion in `delete` — including a re-check that the file's size hasn't changed since it was scanned, as a safeguard against acting on stale information.

### B. The seven commands, in depth

#### `status`
Instant snapshot: hostname, CPU%, RAM used/total, swap used/total, disk used/total, disk utilization %, configured scan root, and config file path. No filesystem walking beyond a single `disk_usage()` call.

#### `report`
Read-only, and significantly lighter than `scan` in what it computes — but it still traverses the filesystem to size folders and find large files, so it is not a quick, shallow check on a large disk (observed run times of 70–85 seconds on a real server in testing). What it specifically skips is duplicate-hashing and cleanup-candidate classification, which is `scan`'s job. Steps:
1. Collects CPU%, load average, RAM/swap, uptime, and OS/kernel/architecture info.
2. Gets root disk usage (total/used/free/utilization%) and flags overall status as `HEALTHY` (<80%), `WARNING` (≥80%), or `CRITICAL` (≥90%).
3. Lists real mounted filesystems via `findmnt`, skipping any filesystem type listed in `excluded_filesystems`.
4. Computes the size of every top-level directory under `scan_root` (e.g. `/usr`, `/var`, `/opt`, `/home`) by fully recursing into each one, and lists the largest `largest_folders` of them.
5. Walks the whole tree (skipping excluded paths and symlinks) to find the largest individual files ≥ `minimum_file_size_mb`, keeping only the top `largest_files`, and looks up each file's owning user.
6. Prints all of this with a 5-section layout: **Server Health**, **Storage**, **Real Filesystems**, **Largest Top-Level Folders**, **Largest Files** — plus a header (hostname/OS/kernel/arch) before section 1.
7. Also writes a simplified plain-text version of the same information to `report_file` (`storage_report.txt`).
8. This command does **not** perform duplicate detection, does **not** identify cleanup candidates, and does **not** write to the pending-actions file — that's entirely `scan`'s job. Protected paths (`server.protected_paths`) are not excluded from this walk — they can still show up in the largest-folders/largest-files listings. "Protected" only means cleanup candidates can't be generated for them and `delete` refuses to touch them; it does not mean they're hidden from reporting.

#### `scan`
The expensive, deep operation. Still makes **no deletions** — it only writes candidates to the pending-actions CSV. Steps:
1. Walks every file under `scan_root` (skipping excluded paths and symlinks), printing progress every 10,000 files.
2. For each file, checks it against the enabled categories, **in this priority order** (a file is only classified as the first category it matches):
   - **`trash`** — inside a `.local/share/Trash` directory (LOW risk).
   - **`cache`** — inside a recognized cache path: `.cache/`, `cache/`, `pip/cache/`, `.npm/`, `.gradle/caches/`, `.cargo/registry/cache/`, or a conda `pkgs/` directory (LOW risk).
   - **`temporary`** — files under `/tmp/` or `/var/tmp/` (LOW risk). The code's `temporary` check also independently tests for `.cache/` in the path, but since `cache` is checked first in the priority order, any `.cache/` file is classified as `cache` before this check ever runs — in practice `temporary` only ever fires for `/tmp/` and `/var/tmp/` files.
   - **`old_log`** — a `.log` file whose modification time is older than `old_log_days` (MEDIUM risk).
   - Each of these is independently gated by both its `scan.find_*` flag and its `cleanup.allow_*` flag being true, and the file must not be under a `protected_path`.
3. **Separately** (not mutually exclusive with the above), any file at or above `duplicate_min_size_mb` — and not protected or protected-extension — is added to a same-size bucket for duplicate detection, provided `find_duplicates`/`allow_duplicates` are on and it doesn't exceed `max_duplicate_file_gb` (if that limit is set).
4. **Duplicate detection** then runs in three stages to avoid hashing everything:
   - Group files that share an exact size.
   - Within each size group, compute a fast partial hash (first `partial_hash_bytes` bytes, SHA-256) and re-group.
   - Within each partial-hash group, compute a full SHA-256 hash of the whole file. Files that fully match are true duplicates; the first one found is kept as the "original," and every other copy becomes a `duplicate` candidate (MEDIUM risk) whose reason names the original file.
5. Every candidate is filtered one final time against `protected_paths` and `protected_extensions` before being saved.
6. Writes the full candidate list to `pending_file` (`pending_actions.csv`), overwriting any previous list.
7. Prints a summary: total files scanned, total candidates found, total potential space, and a breakdown by category (e.g. `cache`, `duplicate`, `old_log`, `temporary`), and appends a line to the history log.

#### `pending`
The one-by-one interactive review of everything `scan` found, shown like this for each candidate:
```
ID:          1
Type:        old_log
Size:        197 B
Risk:        MEDIUM
Path:        /usr/local/src/mqtt_to_http.log
Reason:      Log file older than 30 days.

Delete this file? [y/N/s/q]:
```
- **`y`** — approve this specific file for deletion.
- **`n`** (or just pressing Enter) — explicitly reject it; its approval flag is set to `False`.
- **`s`** — skip it for now without changing its current approval state, and move on; it will be shown again next time you run `pending`.
- **`q`** — stop the review immediately, saving whatever decisions you've made so far.

Note that `n` and `s` behave the same for a candidate you're reviewing for the first time (both leave it unapproved), but they differ if you're re-reviewing a candidate you'd previously approved: `n` explicitly revokes that approval, while `s` leaves the earlier `y` decision in place untouched.

Already-deleted candidates from a previous `delete` run are not shown again. After the loop ends (or you quit), the CSV is saved and a count of "approved for deletion" is printed, along with a reminder to run `./sentinel.py delete`. A line is appended to the history log recording how many were approved.

#### `delete`
1. Loads the pending-actions file and filters to candidates that are `approved=True` and not yet `deleted`.
2. If there's nothing approved, it tells you to run `pending` first and stops.
3. Prints every approved candidate (ID, type, size, path) and the total estimated space to be freed, then asks for one final `y/N` confirmation.
4. For each approved candidate, in order, it re-validates before touching anything:
   - Skips (blocks) if the path is under a `protected_path`.
   - Skips (blocks) if the path has a `protected_extension`.
   - If the file no longer exists, marks it as deleted without error (nothing to do).
   - Skips (blocks) if the path is not a regular file.
   - **Re-checks the file's current size against the size recorded at scan time** — if it changed, the deletion is blocked rather than risking deleting a different/newer file than what was reviewed.
   - Re-checks the protected-extension rule one more time immediately before calling `unlink()`.
   - Deletes the file and marks it `deleted=True`.
   - Any `PermissionError`/`FileNotFoundError`/`OSError` during the attempt is caught and counted as failed rather than crashing the run.
5. Saves the updated CSV (so re-running `delete` won't attempt already-deleted items again) and prints a summary: files deleted, failed/blocked count, and total space freed. Appends a line to the history log.

**Important:** `delete` only ever removes individual **files** — there is no directory-level deletion in this version (unlike an `empty_trash`/`conda_clean` style bulk-directory removal you might see in other tools). Every candidate row corresponds to exactly one file.

#### `history`
Prints the full contents of `history.log` — one line per `scan`, `pending`, or `delete` run, each with a timestamp and key stats (files scanned, candidates found, approved count, deleted/failed count, space freed).

### C. What each cleanup category actually means

| Category | Risk | How it's detected |
|---|---|---|
| `trash` | LOW | Path contains `.local/share/Trash/` (or exactly ends with that folder). |
| `cache` | LOW | Path matches a known cache pattern: `.cache/`, `cache/`, `pip/cache/`, `.npm/`, `.gradle/caches/`, `.cargo/registry/cache/`, or a conda `pkgs/` folder. |
| `temporary` | LOW | Path is under `/tmp/` or `/var/tmp/` (a redundant `.cache/` check in the code never fires in practice, since `cache` is matched first). |
| `old_log` | MEDIUM | File ends in `.log` and hasn't been modified in at least `old_log_days` days. |
| `duplicate` | MEDIUM | File is byte-for-byte identical (confirmed by full SHA-256, after a size and partial-hash pre-filter) to another file that was kept as the "original." |

Categories are checked in the fixed priority order **trash → cache → temporary → old_log**, so a file is only ever assigned to one of these four. Duplicate detection is a separate, independent check that runs on every eligible file regardless of whether it already matched one of the four categories above — so a single file can end up with two candidate rows in `pending_actions.csv`: one for its category (e.g. `cache`) and a separate one for `duplicate`, if it also turns out to be an exact copy of another file.

### D. Path safety logic (on the `Config` object)

- `is_excluded(path)` — true if the path is under any `server.excluded_paths` entry; these are never even walked into.
- `is_protected(path)` — true if the path is under any `server.protected_paths` entry; can be reported on, but `scan` will never generate a candidate for it and `delete` will refuse to touch it.
- `is_protected_extension(path)` — true if the file's extension is in `cleanup.protected_extensions`; checked both at candidate-generation time and (twice) at delete time.

### E. System health metrics

Read straight from Linux's `/proc` filesystem plus `findmnt`/`platform`:
- **CPU%** — two samples of `/proc/stat`, 0.2 seconds apart, non-idle time / total time.
- **Load average** — `os.getloadavg()`.
- **Memory/swap** — parsed from `/proc/meminfo`.
- **Uptime** — parsed from `/proc/uptime`, formatted as `Xd Yh Zm Ws`.
- **OS/Kernel/Architecture** — from Python's `platform` module (shown in the `report` header).
- **Real filesystems** — from `findmnt -rn -o TARGET,SOURCE,FSTYPE`, filtered by `excluded_filesystems`.

### F. Data persistence — the files it maintains

| File (config key) | Format | Purpose |
|---|---|---|
| `report_file` (`storage_report.txt`) | Plain text | A saved copy of the latest `report` output. Overwritten each run. |
| `pending_file` (`pending_actions.csv`) | Standard comma-separated CSV, header `id,type,path,size_bytes,risk,reason,approved,deleted` | The current list of cleanup candidates and their one-by-one approval/deletion state. Rewritten by `scan`; updated in place by `pending` and `delete`. Written atomically (via a `.tmp` file that's then renamed into place). |
| `history_file` (`history.log`) | Plain text, one line per run | Append-only audit trail of every `scan`, `pending`, and `delete` invocation with timestamp and summary stats. |
| `log_file` (`sentinel.log`) | Plain text | Detailed operational log (also mirrored to your terminal) — mostly `INFO` progress lines and `WARNING`s for permission-denied paths encountered while walking the filesystem. |

### G. Command-line reference

```
./sentinel.py [command]

Commands:
  status    Fast system-health snapshot
  report    Read-only storage report — still walks the filesystem for folder
            sizes and largest files, but skips duplicate/cleanup analysis
  scan      Expensive deep scan — finds trash/cache/temp/old-log/duplicate
            candidates and writes them to pending_actions.csv (deletes nothing)
  pending   Interactive, one-by-one approval/rejection of scan candidates
  delete    Deletes only the candidates that were explicitly approved
  history   Print the run history log
  help      Show usage (also the default if no command is given)
```
There is no `--config` option in this version — the config file must be named `config.yaml` and live in the same directory as `sentinel.py`.

### H. Exit codes
- `0` — success
- `1` — configuration error or any other unhandled exception
- `130` — interrupted with Ctrl+C

---

## Safety notes worth remembering

- **Nothing is ever deleted without you personally typing `y` for that specific file** in `pending`, followed by a final confirmation in `delete`.
- Protected paths and protected extensions are enforced at multiple points: when candidates are generated (`scan`), and again — twice — right before each file is actually unlinked (`delete`), including a fresh check that the file hasn't changed size since it was scanned.
- `report` and `scan` are cleanly separated by design, but both are read-only walks of the filesystem — `report` sizes folders and finds large files (still takes over a minute on a large disk); `scan` additionally does duplicate hashing and cleanup-candidate classification, which is why it's the slower of the two. Don't expect duplicate detection or cleanup candidates from `report`.
- All deletions in this version are per-file, not per-directory — there's no "empty this whole folder" action; every row in `pending_actions.csv` is exactly one file.
- The `find_cold_data` scan option exists in `config.yaml` but is off by default and isn't currently used to produce candidates — it's reserved for a future version of the cold-directory feature.
- A prior bash-script version of this tool (`sentinel_sh.save`) is included among the uploaded files but is a legacy prototype, not the active tool — use `sentinel.py` with `config.yaml`.